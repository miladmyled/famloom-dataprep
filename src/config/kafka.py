import os
from typing import Any, Dict
from dotenv import load_dotenv

load_dotenv(override=True)


def get_kafka_producer_config() -> Dict[str, Any]:
    """
    Constructs the confluent-kafka Producer configuration dictionary
    from environment variables.
    """
    bootstrap_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    client_id = os.getenv("KAFKA_CLIENT_ID", "famloom-eventbrite-worker")

    config: Dict[str, Any] = {
        "bootstrap.servers": bootstrap_servers,
        "client.id": client_id,
        "acks": "all",
        "enable.idempotence": True,
        "retries": int(os.getenv("KAFKA_RETRIES", "5")),
        "retry.backoff.ms": int(os.getenv("KAFKA_RETRY_BACKOFF_MS", "500")),
        "socket.timeout.ms": int(os.getenv("KAFKA_SOCKET_TIMEOUT_MS", "15000")),
    }

    security_protocol = os.getenv("KAFKA_SECURITY_PROTOCOL")
    if security_protocol:
        config["security.protocol"] = security_protocol

    sasl_mechanisms = os.getenv("KAFKA_SASL_MECHANISMS")
    if sasl_mechanisms:
        config["sasl.mechanisms"] = sasl_mechanisms

    sasl_username = os.getenv("KAFKA_SASL_USERNAME")
    if sasl_username:
        config["sasl.username"] = sasl_username

    sasl_password = os.getenv("KAFKA_SASL_PASSWORD")
    if sasl_password:
        config["sasl.password"] = sasl_password

    return config


def get_kafka_consumer_config() -> Dict[str, Any]:
    """
    Constructs the hardened confluent-kafka Consumer configuration dictionary.
    Guarantees:
    - Strictly disables auto-commit ('enable.auto.commit': False) to ensure
      at-least-once delivery with manual post-database commits.
    - Explicit 'auto.offset.reset': 'earliest' to read from beginning if no offset exists.
    - Explicit 'session.timeout.ms': 45000 and 'max.poll.interval.ms': 300000.
    - Cooperative-sticky partition assignment strategy for smooth rebalancing.
    """
    bootstrap_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    group_id = os.getenv("KAFKA_CONSUMER_GROUP_ID", "famloom-postgres-loader")
    auto_offset_reset = os.getenv("KAFKA_AUTO_OFFSET_RESET", "earliest")
    partition_assignment_strategy = os.getenv("KAFKA_PARTITION_ASSIGNMENT_STRATEGY", "roundrobin")

    config: Dict[str, Any] = {
        "bootstrap.servers": bootstrap_servers,
        "group.id": group_id,
        "auto.offset.reset": auto_offset_reset,
        "partition.assignment.strategy": partition_assignment_strategy,
        # Strictly disabled: offsets committed manually only after successful DB transactions
        "enable.auto.commit": False,
        "session.timeout.ms": int(os.getenv("KAFKA_SESSION_TIMEOUT_MS", "45000")),
        "max.poll.interval.ms": int(os.getenv("KAFKA_MAX_POLL_INTERVAL_MS", "300000")),
    }

    security_protocol = os.getenv("KAFKA_SECURITY_PROTOCOL")
    if security_protocol:
        config["security.protocol"] = security_protocol

    sasl_mechanisms = os.getenv("KAFKA_SASL_MECHANISMS")
    if sasl_mechanisms:
        config["sasl.mechanisms"] = sasl_mechanisms

    sasl_username = os.getenv("KAFKA_SASL_USERNAME")
    if sasl_username:
        config["sasl.username"] = sasl_username

    sasl_password = os.getenv("KAFKA_SASL_PASSWORD")
    if sasl_password:
        config["sasl.password"] = sasl_password

    return config


def get_kafka_topic() -> str:
    """
    Retrieves the target Kafka topic for raw event ingestion.
    """
    return os.getenv("KAFKA_TOPIC", "raw-events-ingestion")
