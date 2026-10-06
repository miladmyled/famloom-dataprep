# Kafka (production)

Production Kafka runs on the k3s server as the Helm release **`famloom-broker`** in namespace
**`kafka`** (chart `bitnami/kafka` **32.4.3**, Kafka 4.0.0, KRaft: one controller, one broker).
It was installed by hand with Helm and is **not** managed by ArgoCD; ArgoCD only manages
`k8s-manifests/` (app `famloom-dataprep-prod`). The full original install values live only in the
cluster's Helm release record; this folder keeps the production overrides that must survive
upgrades.

| File | What |
|---|---|
| `values-prod-overrides.yaml` | broker memory (1Gi request / 2Gi limit) and fixed 1Gi heap |

## Incident 2026-10-06 (why the overrides exist)

The broker container (limit 768Mi, heap 75% of RAM) was `OOMKilled` during start-up recovery of
`raw-events-ingestion` (≈3.4M offsets, built up by the old every-5-minutes scraper) and crash-looped
for 19 days (1,908 restarts). Producers queued messages that were never acknowledged, so no new
City Events reached production. Fixed by patching the StatefulSet with the values in this folder,
deleting the stuck pod, and setting a 7-day retention on the topic. The scraper now runs twice a
day, so the topic grows far more slowly.

## Applying the overrides on a Helm upgrade

Helm is not installed on the server; run it from a machine with cluster access (k3s kubeconfig:
`/etc/rancher/k3s/k3s.yaml`, readable by root). Keep the chart version unless you are upgrading
on purpose, and always pass this file so the memory settings are not reset:

```bash
helm upgrade famloom-broker oci://registry-1.docker.io/bitnamicharts/kafka --version 32.4.3 \
  -n kafka --reuse-values -f infra/kafka/values-prod-overrides.yaml
```

The values in this file were first applied without Helm (equivalent StatefulSet patch):

```bash
sudo k3s kubectl -n kafka patch statefulset famloom-broker-kafka-broker --type strategic -p \
  '{"spec":{"template":{"spec":{"containers":[{"name":"kafka","resources":{"requests":{"cpu":"500m","memory":"1Gi"},"limits":{"cpu":"1","memory":"2Gi"}},"env":[{"name":"KAFKA_HEAP_OPTS","value":"-Xms1g -Xmx1g"}]}]}}}}'
sudo k3s kubectl -n kafka delete pod famloom-broker-kafka-broker-0   # a crash-looping pod is not replaced automatically
```

## Topic settings

`raw-events-ingestion` keeps 7 days of messages (the consumer processes them within seconds):

```bash
sudo k3s kubectl -n kafka exec famloom-broker-kafka-broker-0 -c kafka -- /opt/bitnami/kafka/bin/kafka-configs.sh \
  --bootstrap-server localhost:9092 --alter --entity-type topics --entity-name raw-events-ingestion \
  --add-config retention.ms=604800000
```

## Health checks

```bash
sudo k3s kubectl -n kafka get pods                                   # broker and controller 1/1 Running, restarts stable
sudo k3s kubectl -n kafka describe pod famloom-broker-kafka-broker-0 | grep -A4 "Last State"   # no OOMKilled
sudo k3s kubectl -n kafka exec famloom-broker-kafka-broker-0 -c kafka -- /opt/bitnami/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:9092 --describe --topic raw-events-ingestion          # Leader and Isr present
```

A scraper run that cannot deliver logs `Broker Acknowledged: 0` and `Unflushed Buffer Msg > 0` and
exits with code 1; check the broker first.
