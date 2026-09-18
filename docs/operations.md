# Operations guide

The exporter is designed to be run by a scheduler. It does not send alerts
itself. Operators can connect the scheduler's failure signal to the email,
webhook, or platform alert receiver used by their deployment.

## Scheduler failure signal

A completed exporter run is successful only when `lastfm-export` exits with
status 0. Treat any non-zero exporter exit as the scheduler alert trigger.
This preserves the failure signal even when one configured user fails, and
keeps alert delivery in the scheduler or platform rather than in the
exporter.

### Cron

Invoke the exporter directly from cron and configure the cron host's chosen
failure notification or monitoring integration to observe the command's
non-zero exit:

```cron
# Non-zero exporter exit is the scheduler alert trigger.
15 * * * * /usr/local/bin/lastfm-export --config /etc/lastfm-export/config.toml >>/var/log/lastfm-export.log 2>&1
```

### Kubernetes CronJob

Run the exporter as the `CronJob` container command. A non-zero exporter exit
makes the pod fail, so the Kubernetes scheduler/monitoring integration can use
the failed Job as its alert trigger. Choose and configure the receiver in the
cluster according to local operations practice.

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: lastfm-export
spec:
  schedule: "15 * * * *"
  jobTemplate:
    spec:
      backoffLimit: 0
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: exporter
              image: ghcr.io/example/lastfm-export:latest
              command:
                - lastfm-export
                - --config
                - /etc/lastfm-export/config.toml
```

## Terminal run telemetry

Each configured user receives one terminal structured-log JSON record. The
stable fields are:

| Field | Meaning |
| --- | --- |
| `username` | Configured Last.fm user |
| `outcome` | `success` or `failed` |
| `rows_extracted` | Rows extracted for the run |
| `rows_skipped_now_playing` | In-progress rows omitted because they have no scrobble date |
| `pages_fetched` | Last.fm pages fetched |
| `retry_count` (retries) | Number of retries |
| `retry_causes` | Causes recorded for retries |
| `duration_seconds` (duration) | Run duration in seconds |

These terminal fields provide the trustworthy telemetry to attach to the
operator-selected alert receiver. A failed record also includes a redacted
exception object; secrets are not part of the alert handoff.

## Checkpoint and staleness diagnosis

After a failure alert, run `lastfm-export status --config
/etc/lastfm-export/config.toml`. It emits one JSON record per configured user
with the checkpoint watermark (`last_successful_to`) and
`staleness_seconds`. Use those values to distinguish a failed current run
from a user whose checkpoint has not yet been initialized, and to decide
whether a retry or investigation is needed.
