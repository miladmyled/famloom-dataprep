import os
import json
import time
import logging
from typing import Any, Dict, List, Optional
from confluent_kafka import Consumer, KafkaError, KafkaException, TopicPartition, OFFSET_BEGINNING
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from src.config.kafka import get_kafka_consumer_config, get_kafka_topic
from src.config.database import get_db_pool
from src.db.events import init_db_schema, upsert_city_event
from src.models.event import CityEvent

logger = logging.getLogger(__name__)


class EventKafkaConsumer:
    """
    Production-grade Kafka Consumer for raw-events-ingestion.
    Consumes JSON payloads, validates CityEvent schema, executes idempotent
    PostgreSQL upserts, handles poison pills resiliently, and enforces manual
    post-transaction offset commits for at-least-once delivery semantics.
    """

    def __init__(
        self,
        kafka_config: Optional[Dict[str, Any]] = None,
        topic: Optional[str] = None,
        db_pool: Optional[ConnectionPool] = None,
    ):
        self.config = kafka_config or get_kafka_consumer_config()
        self.topic = topic or get_kafka_topic()
        self.db_pool = db_pool or get_db_pool()
        self.running = False

        # Initialize and verify database schema contracts (TIMESTAMPTZ & columns)
        init_db_schema(self.db_pool)

        # Rebalance callbacks for partition tracking diagnostics
        def on_assign(consumer: Consumer, partitions: List[TopicPartition]) -> None:
            for p in partitions:
                logger.info(f"[CONSUMER REBALANCE] Assigned partition: topic={p.topic} partition={p.partition}")
                if os.getenv("KAFKA_RESET_OFFSET_ON_START", "false").lower() in ("1", "true", "yes"):
                    p.offset = OFFSET_BEGINNING
            consumer.assign(partitions)

        def on_revoke(consumer: Consumer, partitions: List[TopicPartition]) -> None:
            for p in partitions:
                logger.info(f"[CONSUMER REBALANCE] Revoked partition: topic={p.topic} partition={p.partition}")
            consumer.unassign()

        # Initialize confluent-kafka Consumer
        try:
            self.consumer = Consumer(self.config)

            # Verify connectivity to Kafka cluster immediately on boot
            broker_addr = self.config.get("bootstrap.servers")
            logger.info(f"[CONSUMER] Verifying connectivity to Kafka broker '{broker_addr}'...")
            try:
                cluster_meta = self.consumer.list_topics(timeout=10.0)
                avail_topics = list(cluster_meta.topics.keys())
                logger.info(
                    f"✅ [CONSUMER] Broker connection verified! Nodes: {len(cluster_meta.brokers)}, "
                    f"Cluster topics: {avail_topics}"
                )
                if self.topic not in cluster_meta.topics:
                    logger.warning(
                        f"⚠️ [CONSUMER] Target topic '{self.topic}' not yet found on broker. Existing topics: {avail_topics}"
                    )
            except Exception as conn_err:
                logger.error(
                    f"❌ [CONSUMER CONNECTION FAILED] Unable to reach Kafka broker at '{broker_addr}': {conn_err}\n"
                    f"Please verify KAFKA_BOOTSTRAP_SERVERS address and cluster network DNS/routing."
                )
                raise

            use_direct_assignment = os.getenv("KAFKA_DIRECT_ASSIGN", "true").lower() in ("1", "true", "yes")
            if use_direct_assignment:
                reset_on_start = os.getenv("KAFKA_RESET_OFFSET_ON_START", "true").lower() in ("1", "true", "yes")
                initial_offset = OFFSET_BEGINNING if reset_on_start else confluent_kafka.OFFSET_STORED
                tp = TopicPartition(self.topic, 0, initial_offset)
                self.consumer.assign([tp])
                logger.info(
                    f"🎯 [CONSUMER] Direct partition assignment active: topic='{self.topic}' partition=0 "
                    f"(initial_offset={initial_offset}, reset_to_beginning={reset_on_start}). Ready to consume!"
                )
            else:
                self.consumer.subscribe([self.topic], on_assign=on_assign, on_revoke=on_revoke)
                logger.info(f"[CONSUMER] Subscribed to topic '{self.topic}' using dynamic group rebalance.")

            logger.info(
                f"[CONSUMER] Initialized Consumer (Group: '{self.config.get('group.id')}', "
                f"Brokers: '{self.config.get('bootstrap.servers')}', "
                f"auto.offset.reset: '{self.config.get('auto.offset.reset')}') "
                f"subscribed to topic '{self.topic}'"
            )
        except Exception as e:
            logger.error(f"[CONSUMER] Failed to initialize Kafka Consumer: {e}")
            raise

    def process_message(self, msg: Any) -> bool:
        """
        Processes a single Kafka message:
        1. Deserializes JSON and validates CityEvent Pydantic model.
        2. Emits verbose debug telemetry (topic, partition, offset, key, size).
        3. Handles Poison Pills by logging and committing offset immediately.
        4. Executes PostgreSQL transaction upsert.
        5. Manually commits Kafka offset upon DB success.

        Returns:
            bool: True if message processed/committed successfully, False if transient retry needed.
        """
        # 1. Check for Kafka broker/partition errors
        if msg.error():
            if msg.error().code() == KafkaError._PARTITION_EOF:
                # End of partition event, not an error
                logger.debug(
                    f"[CONSUMER] Reached EOF at topic {msg.topic()} "
                    f"[{msg.partition()}] offset {msg.offset()}"
                )
                return True
            else:
                logger.error(f"[CONSUMER] Kafka message error: {msg.error()}")
                return False

        # 2. Extract and decode message payload
        raw_value = msg.value()
        if raw_value is None:
            # Tombstone message (null payload)
            logger.info(
                f"[CONSUMER] Received null payload (tombstone) at partition={msg.partition()} "
                f"offset={msg.offset()}. Committing offset."
            )
            self.consumer.commit(message=msg, asynchronous=False)
            return True

        msg_str = raw_value.decode("utf-8", errors="replace")
        msg_key = msg.key().decode("utf-8", errors="replace") if msg.key() is not None else "None"
        msg_size = len(raw_value)
        partition = msg.partition()
        offset = msg.offset()

        # Verbose debug log for Kubernetes telemetry tracking
        logger.info(
            f"[CONSUMER DEBUG] Received message: topic={msg.topic()} partition={partition} "
            f"offset={offset} key={msg_key} size_bytes={msg_size}"
        )

        # 3. Poison Pill Handling: JSON Deserialization & Pydantic Validation
        try:
            payload_dict = json.loads(msg_str)
            event = CityEvent(**payload_dict)
        except (json.JSONDecodeError, ValidationError, Exception) as val_err:
            # Poison pill detected! Log detailed context and commit offset to prevent infinite retry loops.
            logger.warning(
                f"☣️ [POISON PILL] Corrupt message dropped at topic {msg.topic()} "
                f"[{partition}] @ offset {offset}: {val_err}\n"
                f"Payload snippet: {msg_str[:250]}"
            )
            # Commit offset to advance consumer past poison pill
            self.consumer.commit(message=msg, asynchronous=False)
            return True

        # 4. Database Transaction & Idempotent Upsert
        try:
            with self.db_pool.connection() as conn:
                with conn.transaction():
                    upsert_city_event(conn, event)

            # 5. Manual Offset Commit (Strictly after DB transaction commit)
            self.consumer.commit(message=msg, asynchronous=False)
            logger.info(
                f"✅ [LOADED] Event '{event.title}' (ID: {event.event_id}, City: {event.city}) "
                f"persisted to PostgreSQL. Partition {partition} offset {offset} committed."
            )
            return True

        except Exception as db_err:
            # Transient DB error: Do NOT commit offset so Kafka redelivers upon recovery
            logger.error(
                f"❌ [DB ERROR] Failed to persist event '{event.event_id}' at offset {offset}: {db_err}. "
                "Offset will NOT be committed (at-least-once retry)."
            )
            return False

    def run(self, poll_timeout: float = 1.0) -> None:
        """
        Main polling loop for the consumer worker.
        Runs until self.running is set to False (via signal handler).
        """
        self.running = True
        logger.info(f"🚀 [CONSUMER] Starting main polling loop on topic '{self.topic}'...")

        idle_polls = 0
        while self.running:
            try:
                msg = self.consumer.poll(timeout=poll_timeout)
                if msg is None:
                    idle_polls += 1
                    # Heartbeat log every 30 seconds of idle polling at INFO level for kubectl logs
                    if idle_polls >= 30:
                        logger.info(
                            f"[CONSUMER HEARTBEAT] Polling active on topic '{self.topic}' "
                            f"(idle for {idle_polls}s). Awaiting incoming messages..."
                        )
                        idle_polls = 0
                    continue

                idle_polls = 0
                success = self.process_message(msg)
                if not success:
                    # Brief backoff before retrying on transient database failures
                    time.sleep(0.5)

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
            logger.info("🔌 [CONSUMER] Kafka consumer closed cleanly (left consumer group).")
        except Exception as ce:
            logger.error(f"[CONSUMER] Error closing Kafka consumer: {ce}")

        try:
            self.db_pool.close()
            logger.info("🔌 [DB] Database connection pool closed cleanly.")
        except Exception as de:
            logger.error(f"[DB] Error closing DB pool: {de}")
