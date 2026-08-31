import json
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
    with idempotency keys, and handles asynchronous delivery report callbacks.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        topic: Optional[str] = None,
    ):
        self.config = config or get_kafka_producer_config()
        self.topic = topic or get_kafka_topic()
        
        try:
            self.producer = Producer(self.config)
            logger.info(
                f"[KAFKA] Initialized Producer for topic '{self.topic}' "
                f"(Brokers: {self.config.get('bootstrap.servers')})"
            )
        except Exception as e:
            logger.error(f"[KAFKA] Failed to initialize Confluent Kafka Producer: {e}")
            raise

    @staticmethod
    def delivery_report(err: Optional[KafkaError], msg: Any) -> None:
        """
        Asynchronous callback invoked by confluent-kafka once a message
        has been successfully delivered or failed permanently.
        """
        if err is not None:
            logger.error(
                f"[KAFKA] Delivery FAILED for record key='{msg.key() if msg else 'None'}': {err}"
            )
        else:
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

    def flush(self, timeout: float = 10.0) -> int:
        """
        Flushes any outstanding messages in the producer buffer.
        Must be called prior to process termination to guarantee zero message loss.

        Returns:
            int: Number of messages still un-flushed upon timeout (0 indicates full success).
        """
        logger.info(f"[KAFKA] Flushing producer buffer (Timeout: {timeout}s)...")
        remaining = self.producer.flush(timeout)
        if remaining > 0:
            logger.warning(f"[KAFKA] Flush timeout reached. {remaining} messages remained in buffer.")
        else:
            logger.info("[KAFKA] Producer buffer successfully flushed. All records delivered.")
        return remaining
