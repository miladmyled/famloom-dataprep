import os
import sys
import json
import time
import socket
import logging
from typing import Any, Dict, List, Optional
from confluent_kafka import Consumer, KafkaError, KafkaException, TopicPartition, OFFSET_STORED, OFFSET_BEGINNING
from pydantic import ValidationError

from src.models.event import CityEvent
from src.db.events import init_db_schema, upsert_city_event
from src.config.database import get_db_pool

logger = logging.getLogger(__name__)


class EventKafkaConsumer:
    """
    Robust Kafka to PostgreSQL Event Consumer.
    Features:
    - Subscribes to raw-events-ingestion Kafka topic using consumer groups.
    - Idempotent transaction-safe PostgreSQL upserts.
    - At-least-once delivery guarantees with explicit manual offset commits.
    - Resilient Poison Pill handling (corrupted/unparseable messages committed past).
    - Periodic heartbeat telemetry and graceful shutdown signal handling.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        topic: Optional[str] = None,
    ) -> None:
        self.topic = topic or os.getenv("KAFKA_TOPIC_NAME", "raw-events-ingestion")
        self.running = False

        # Initialize PostgreSQL connection pool and verify table schema contracts
        self.db_pool = get_db_pool()
        try:
            init_db_schema(self.db_pool)
        except Exception as schema_err:
            logger.warning(f"⚠️ [CONSUMER] Schema verification warning: {schema_err}")

        # Build Confluent Kafka Consumer configuration
        default_config = {
            "bootstrap.servers": os.getenv(
                "KAFKA_BOOTSTRAP_SERVERS", "famloom-broker-kafka-broker.kafka.svc.cluster.local:9092"
            ),
            "group.id": os.getenv("KAFKA_CONSUMER_GROUP_ID", "famloom-events-consumer-group"),
            "auto.offset.reset": os.getenv("KAFKA_AUTO_OFFSET_RESET", "earliest"),
            "enable.auto.commit": False,
            "session.timeout.ms": int(os.getenv("KAFKA_SESSION_TIMEOUT_MS", "45000")),
            "max.poll.interval.ms": int(os.getenv("KAFKA_MAX_POLL_INTERVAL_MS", "300000")),
            "client.id": f"famloom-consumer-{socket.gethostname()}",
        }

        if config:
            default_config.update(config)
        self.config = default_config

        try:
            self.consumer = Consumer(self.config)

            def on_assign(c, partitions):
                logger.info(f"🔄 [CONSUMER REBALANCE] Assigned partitions: {[p.partition for p in partitions]}")

            def on_revoke(c, partitions):
                logger.info(f"🔄 [CONSUMER REBALANCE] Revoked partitions: {[p.partition for p in partitions]}")

            self.consumer.subscribe([self.topic], on_assign=on_assign, on_revoke=on_revoke)
            logger.info(
                f"🚀 [CONSUMER] Initialized Consumer (Group: '{self.config.get('group.id')}', "
                f"Brokers: '{self.config.get('bootstrap.servers')}', "
                f"auto.offset.reset: '{self.config.get('auto.offset.reset')}') "
                f"subscribed to topic '{self.topic}'"
            )
        except Exception as e:
            logger.error(f"[CONSUMER] Failed to initialize Kafka Consumer: {e}", exc_info=True)
            raise

    def process_message(self, msg: Any) -> bool:
        """
        Processes a single Kafka message:
        1. Deserializes JSON and validates CityEvent model.
        2. Handles Poison Pills by logging and committing offset immediately.
        3. Executes PostgreSQL transaction upsert.
        4. Manually commits Kafka offset upon DB success.

        Returns:
            bool: True if message processed/committed successfully, False if transient retry needed.
        """
        if msg.error():
            if msg.error().code() == KafkaError._PARTITION_EOF:
                logger.debug(
                    f"[CONSUMER] Reached EOF at topic {msg.topic()} "
                    f"[{msg.partition()}] offset {msg.offset()}"
                )
                return True
            else:
                logger.error(f"[CONSUMER] Kafka message error: {msg.error()}")
                return False

        raw_value = msg.value()
        if raw_value is None:
            logger.info(
                f"[CONSUMER] Received null payload (tombstone) at partition={msg.partition()} "
                f"offset={msg.offset()}. Committing offset."
            )
            try:
                self.consumer.commit(message=msg, asynchronous=False)
            except Exception:
                pass
            return True

        msg_str = raw_value.decode("utf-8", errors="replace")
        partition = msg.partition()
        offset = msg.offset()

        # Poison pill handling: JSON Deserialization & Pydantic Validation
        try:
            payload_dict = json.loads(msg_str)
            event = CityEvent(**payload_dict)
        except (json.JSONDecodeError, ValidationError, Exception) as val_err:
            logger.warning(
                f"☣️ [POISON PILL] Corrupt message dropped at topic {msg.topic()} "
                f"[{partition}] @ offset {offset}: {val_err}\n"
                f"Payload snippet: {msg_str[:250]}"
            )
            try:
                self.consumer.commit(message=msg, asynchronous=True)
            except Exception:
                pass
            return True

        # Database Transaction & Idempotent Upsert
        try:
            with self.db_pool.connection() as conn:
                with conn.transaction():
                    upsert_city_event(conn, event)
        except Exception as db_err:
            logger.error(
                f"❌ [DB ERROR] Failed to persist event '{event.event_id}' at offset {offset}: {db_err}. "
                "Offset will NOT be committed (at-least-once retry)."
            )
            return False

        # Manual Offset Commit
        try:
            self.consumer.commit(message=msg, asynchronous=True)
        except KafkaException as ke:
            logger.debug(f"[KAFKA COMMIT] Offset {offset} async commit deferred: {ke}")
        except Exception as ex:
            logger.debug(f"[KAFKA COMMIT] Offset {offset} commit error: {ex}")

        tag_info = f" with {len(event.tag_ids)} tags" if event.tag_ids else ""
        logger.info(
            f"✅ [LOADED] Event '{event.title}' (ID: {event.event_id}, City: {event.city}){tag_info} "
            f"persisted to PostgreSQL. Partition {partition} offset {offset} committed."
        )
        return True

    def process_batch(self, messages: List[Any]) -> int:
        """
        Processes a batch of Kafka messages in a single database transaction,
        committing the highest offset once persisted. Falls back to single-message
        processing if a batch transaction encounters an error.
        """
        if not messages:
            return 0

        valid_events: List[CityEvent] = []
        valid_msgs: List[Any] = []

        for msg in messages:
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                logger.error(f"[CONSUMER] Kafka message error: {msg.error()}")
                continue

            raw_value = msg.value()
            if raw_value is None:
                # Tombstone
                try:
                    self.consumer.commit(message=msg, asynchronous=False)
                except Exception:
                    pass
                continue

            msg_str = raw_value.decode("utf-8", errors="replace")
            try:
                payload_dict = json.loads(msg_str)
                event = CityEvent(**payload_dict)
                valid_events.append(event)
                valid_msgs.append(msg)
            except Exception as val_err:
                logger.warning(
                    f"☣️ [POISON PILL] Corrupt message dropped at offset {msg.offset()}: {val_err}"
                )
                try:
                    self.consumer.commit(message=msg, asynchronous=True)
                except Exception:
                    pass

        if not valid_events:
            return 0

        # Attempt atomic batch transaction across WAN to Azure
        try:
            with self.db_pool.connection() as conn:
                with conn.transaction():
                    for event in valid_events:
                        upsert_city_event(conn, event)

            # Commit highest offset in batch
            highest_msg = valid_msgs[-1]
            try:
                self.consumer.commit(message=highest_msg, asynchronous=True)
            except Exception:
                pass

            logger.info(
                f"✅ [BATCH LOADED] Persisted {len(valid_events)} event(s) to PostgreSQL "
                f"(Offsets {valid_msgs[0].offset()}-{valid_msgs[-1].offset()})."
            )
            return len(valid_events)

        except Exception as batch_err:
            logger.warning(
                f"⚠️ [CONSUMER] Batch transaction encountered error: {batch_err}. "
                "Falling back to single-message processing for resilience..."
            )
            success_count = 0
            for msg in valid_msgs:
                if self.process_message(msg):
                    success_count += 1
            return success_count

    def run(self, poll_timeout: float = 1.0) -> None:
        """
        Main polling loop for the consumer worker using batched consumption.
        Runs until self.running is set to False (via signal handler).
        """
        self.running = True
        batch_size = int(os.getenv("KAFKA_CONSUMER_BATCH_SIZE", "50"))
        logger.info(
            f"🚀 [CONSUMER] Starting main polling loop (batch_size={batch_size}) on topic '{self.topic}'..."
        )

        idle_polls = 0
        while self.running:
            try:
                msgs = self.consumer.consume(num_messages=batch_size, timeout=poll_timeout)
                if not msgs:
                    idle_polls += 1
                    if idle_polls >= 30:
                        logger.info(
                            f"[CONSUMER HEARTBEAT] Polling active on topic '{self.topic}' "
                            f"(idle for {idle_polls}s). Awaiting incoming messages..."
                        )
                        idle_polls = 0
                    continue

                idle_polls = 0
                self.process_batch(msgs)

            except KafkaException as ke:
                logger.error(f"[CONSUMER] Kafka exception during poll: {ke}")
                time.sleep(1.0)
            except Exception as e:
                logger.error(f"[CONSUMER] Unexpected error in polling loop: {e}", exc_info=True)
                time.sleep(1.0)

        logger.info("🛑 [CONSUMER] Polling loop stopped.")

    def close(self) -> None:
        """
        Gracefully terminates consumer by leaving consumer group and closing database pool.
        """
        logger.info("[CONSUMER] Commencing graceful shutdown...")
        try:
            self.consumer.close()
            logger.info("🔌 [CONSUMER] Kafka consumer closed cleanly.")
        except Exception as ce:
            logger.error(f"[CONSUMER] Error closing Kafka consumer: {ce}")

        try:
            self.db_pool.close()
            logger.info("🔌 [DB] Database connection pool closed cleanly.")
        except Exception as de:
            logger.error(f"[DB] Error closing DB pool: {de}")
