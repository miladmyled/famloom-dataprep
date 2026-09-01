import time
import logging
from typing import Any, Dict, Optional
from confluent_kafka import Producer, KafkaError, KafkaException

from src.config.kafka import get_kafka_producer_config, get_kafka_topic
from src.models.event import CityEvent

logger = logging.getLogger(__name__)


class EventKafkaProducer:
    """
    Production-ready Kafka Producer leveraging confluent-kafka.
    Serializes validated CityEvent instances to JSON, produces messages
    with idempotency keys, tracks delivery acknowledgment metrics,
    and enforces robust buffer flushing with retries.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        topic: Optional[str] = None,
    ):
        self.config = config or get_kafka_producer_config()
        self.topic = topic or get_kafka_topic()
        self.delivered_count = 0
        self.failed_count = 0
        
        try:
            self.producer = Producer(self.config)
            logger.info(
                f"[KAFKA] Initialized Producer for topic '{self.topic}' "
                f"(Brokers: {self.config.get('bootstrap.servers')})"
            )
        except Exception as e:
            logger.error(f"[KAFKA] Failed to initialize Confluent Kafka Producer: {e}")
            raise

    def delivery_report(self, err: Optional[KafkaError], msg: Any) -> None:
        """
        Asynchronous callback invoked by confluent-kafka once a message
        has been successfully delivered to the broker or failed permanently.
        """
        if err is not None:
            self.failed_count += 1
            logger.error(
                f"[KAFKA] Delivery FAILED for record key='{msg.key() if msg else 'None'}': {err}"
            )
        else:
            self.delivered_count += 1
            key_str = msg.key().decode("utf-8") if msg.key() else "None"
            logger.info(
                f"[KAFKA] Delivered to topic '{msg.topic()}' [partition {msg.partition()}] "
                f"at offset {msg.offset()} (key={key_str})"
            )

    def publish_event(self, event: CityEvent) -> bool:
        """
        Serializes a validated CityEvent object into JSON bytes and publishes it
        to the configured Kafka topic, using the event_id as the message key.

        Returns:
            bool: True if queued successfully into the local buffer, False otherwise.
        """
        try:
            payload = event.model_dump_json().encode("utf-8")
            key = event.event_id.encode("utf-8")

            self.producer.produce(
                topic=self.topic,
                key=key,
                value=payload,
                on_delivery=self.delivery_report,
            )

            # Serve delivery callback events from previous produce calls
            self.producer.poll(0)
            return True

        except BufferError as e:
            logger.warning(f"[KAFKA] Local producer buffer full. Flushing queue: {e}")
            self.producer.poll(0.5)
            # Retry produce once after polling
            try:
                self.producer.produce(
                    topic=self.topic,
                    key=event.event_id.encode("utf-8"),
                    value=event.model_dump_json().encode("utf-8"),
                    on_delivery=self.delivery_report,
                )
                return True
            except Exception as retry_err:
                logger.error(f"[KAFKA] Failed to produce event {event.event_id} after buffer flush: {retry_err}")
                return False

        except KafkaException as ke:
            logger.error(f"[KAFKA] Kafka exception publishing event {event.event_id}: {ke}")
            return False
        except Exception as ex:
            logger.error(f"[KAFKA] Unexpected error serializing/publishing event {event.event_id}: {ex}")
            return False

    def flush(self, timeout: float = 30.0, max_attempts: int = 3) -> int:
        """
        Flushes outstanding messages from librdkafka's internal buffer across
        the network to Kafka brokers. Retries automatically if initial timeout is reached.

        Returns:
            int: Number of messages still un-flushed upon final timeout (0 indicates 100% success).
        """
        logger.info(f"[KAFKA] Flushing producer buffer (Timeout per attempt: {timeout}s)...")
        start_time = time.time()
        remaining = len(self.producer)

        for attempt in range(1, max_attempts + 1):
            remaining = self.producer.flush(timeout)
            if remaining == 0:
                elapsed = time.time() - start_time
                logger.info(
                    f"✅ [KAFKA] Producer buffer successfully drained in {elapsed:.2f}s! "
                    f"All {self.delivered_count} records acknowledged by broker."
                )
                return 0
            
            logger.warning(
                f"⚠️ [KAFKA] Flush attempt {attempt}/{max_attempts} timed out. "
                f"{remaining} messages still in buffer."
            )
            # Serve any pending events before retrying
            self.producer.poll(1.0)

        logger.error(
            f"❌ [KAFKA] Critical: Failed to flush {remaining} messages after "
            f"{max_attempts} attempts ({time.time() - start_time:.2f}s total)."
        )
        return remaining

    def get_delivery_metrics(self) -> Dict[str, int]:
        """Returns confirmed delivery acknowledgment counters."""
        return {
            "delivered": self.delivered_count,
            "failed": self.failed_count,
            "buffered": len(self.producer),
        }
