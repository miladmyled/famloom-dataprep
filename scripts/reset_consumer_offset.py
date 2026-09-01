"""
Administrative Script: Reset Kafka Consumer Group Offsets to Earliest
Usage:
    python scripts/reset_consumer_offset.py [--topic TOPIC] [--group GROUP_ID] [--partition PARTITION]

Resets the committed offset for the specified consumer group to the earliest available
offset on all assigned partitions of the topic (or a specific partition, default: all/0).
"""

import sys
import argparse
import logging
from confluent_kafka import Consumer, TopicPartition, OFFSET_BEGINNING
from dotenv import load_dotenv

load_dotenv(override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("OffsetResetAdmin")

from src.config.kafka import get_kafka_consumer_config, get_kafka_topic


def reset_offsets(topic: str, group_id: str, partition_id: int = -1) -> bool:
    """
    Safely resets the committed offsets for group_id on topic to OFFSET_BEGINNING.
    """
    config = get_kafka_consumer_config()
    if group_id:
        config["group.id"] = group_id

    # Do not auto-commit
    config["enable.auto.commit"] = False

    logger.info(f"Connecting to Kafka at {config.get('bootstrap.servers')} as group '{config.get('group.id')}'...")
    consumer = Consumer(config)

    try:
        # Fetch topic metadata to discover partitions
        metadata = consumer.list_topics(topic, timeout=10.0)
        if topic not in metadata.topics:
            logger.error(f"Topic '{topic}' not found in Kafka cluster!")
            return False

        topic_meta = metadata.topics[topic]
        available_partitions = list(topic_meta.partitions.keys())
        logger.info(f"Discovered partitions for topic '{topic}': {available_partitions}")

        if partition_id >= 0:
            if partition_id not in available_partitions:
                logger.error(f"Requested partition {partition_id} not found in topic '{topic}'!")
                return False
            target_partitions = [partition_id]
        else:
            target_partitions = available_partitions

        # Query low/high watermarks for each partition
        partitions_to_commit = []
        for p in target_partitions:
            tp = TopicPartition(topic, p)
            low, high = consumer.get_watermark_offsets(tp, timeout=10.0, cached=False)
            logger.info(f"Partition {p} watermarks: low={low} (earliest), high={high} (latest)")
            
            # Prepare TopicPartition with OFFSET_BEGINNING
            reset_tp = TopicPartition(topic, p, OFFSET_BEGINNING)
            partitions_to_commit.append(reset_tp)

        # Assign partitions directly to avoid needing an active group rebalance
        consumer.assign(partitions_to_commit)

        # Commit OFFSET_BEGINNING for the consumer group
        consumer.commit(offsets=partitions_to_commit, asynchronous=False)

        # Verify new committed offsets
        committed = consumer.committed(partitions_to_commit, timeout=10.0)
        for tp in committed:
            logger.info(
                f"✅ Successfully reset and committed offset for topic='{tp.topic}' "
                f"partition={tp.partition} offset={tp.offset}"
            )

        logger.info(
            f"🎉 Consumer group '{config.get('group.id')}' successfully reset to earliest available offsets!"
        )
        return True

    except Exception as e:
        logger.error(f"Failed to reset consumer offsets: {e}", exc_info=True)
        return False
    finally:
        consumer.close()


def main():
    parser = argparse.ArgumentParser(description="Reset Kafka Consumer Group Offsets to Earliest")
    parser.add_argument("--topic", default=get_kafka_topic(), help="Kafka topic name")
    parser.add_argument("--group", default=None, help="Kafka consumer group.id (defaults to KAFKA_CONSUMER_GROUP_ID)")
    parser.add_argument("--partition", type=int, default=-1, help="Specific partition ID (-1 for all partitions)")
    args = parser.parse_args()

    success = reset_offsets(topic=args.topic, group_id=args.group, partition_id=args.partition)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
