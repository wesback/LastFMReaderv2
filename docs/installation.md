# Installation and unattended operation

## Install the exporter

Clone or check out the repository, then install the core package in editable
mode when writing to local storage or a destination whose filesystem adapter
is already available:

```console
python3 -m pip install -e .
```

Install the AWS extra for S3 destinations:

```console
python3 -m pip install -e '.[aws]'
```

Install the Azure extra for Azure Blob Storage or ADLS Gen2 destinations:

```console
python3 -m pip install -e '.[azure]'
```

The extras are optional. The core package contains the exporter and local
filesystem support; the `aws` extra adds `s3fs` and `boto3`, and the `azure`
extra adds `adlfs` and `azure-identity`.

Do not put API keys, access keys, connection strings, SAS tokens, or other
cloud credentials in the TOML file, source tree, container image, or command
line. The TOML `api_key_env` setting names the environment variable from which
the Last.fm API key is read. Supply that variable through the host's secret
manager or the scheduler's secret injection. Cloud adapters use their native credential chains:
AWS uses the standard boto3 chain, and Azure uses
`DefaultAzureCredential` (managed identity, workload identity, or another
credential supported by the deployment).
In short, supply API keys through environment variables and cloud credentials
through native credential chains rather than committed configuration.

Copy [config.example.toml](config.example.toml) to a deployment-owned path,
replace its example usernames and destinations, and keep the copied file free
of credential values. A normal unattended run is:

```console
lastfm-export --config /etc/lastfm-export/config.toml
```

The command exits non-zero when any configured user fails. Cron, Kubernetes
CronJob, and other schedulers should use that exit status as their failure
signal; see the [operations guide](operations.md).
