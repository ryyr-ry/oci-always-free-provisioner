# OCI Always Free A1 provisioner

Provision exactly one Ubuntu 24.04 ARM64 instance when home-region A1 capacity becomes available.

## Requested configuration

- Shape: `VM.Standard.A1.Flex`
- CPU: 2 OCPU
- Memory: 12 GB
- Boot volume: 50 GB, balanced performance
- Data volume: 100 GB, balanced performance, paravirtualized attachment
- IPv4: reserved public address assigned to the primary private IP
- Guest controller: low-priority CPU and resident-memory targets of 25%

The 150 GB total includes both volumes. The data volume is attached but is **not formatted or mounted** by this workflow. Filesystem setup and Cloudflare administration access are separate tasks. No Hermes installation is included.

## Scheduling and retries

`.github/workflows/provision.yml` runs hourly at minute 17 and supports manual dispatch. Each run retries for up to 60 minutes, at intervals of at least 60 seconds measured from the start of the preceding attempt.

Only one provisioning job can run at a time. A following hourly run waits instead of canceling an active run. GitHub schedules are best-effort and may be delayed; this is not a real-time scheduler. GitHub can also disable scheduled workflows in public repositories after 60 days without repository activity.

After all resources are ready, the workflow:

1. Saves a `provisioning-result` artifact containing resource identifiers and the public IPv4, but no credentials.
2. Opens one success issue mentioning the repository owner.
3. Disables its own provisioning workflow to stop future scheduled attempts.

To start a fresh retry window manually:

```bash
gh workflow run provision.yml --repo OWNER/REPOSITORY
```

To stop attempts:

```bash
gh workflow disable provision.yml --repo OWNER/REPOSITORY
```

Disabling a workflow prevents new runs. To stop an already running job, cancel that run in GitHub Actions or use `gh run cancel RUN_ID`.

## Idempotency and allocation checks

- A stable provisioning tag identifies managed resources across runners and workflow runs.
- Instance and volume creation use stable OCI retry tokens.
- Every attempt looks for existing resources before issuing creation requests.
- A stopped, terminated, or mismatched managed instance is not automatically replaced.
- A reserved address assigned to another private IP is never moved automatically.
- Instance creation requires enough unused Always Free volume capacity for the full 150 GB configuration.
- Additional A1 allocations in the configured compartment stop creation rather than exceeding the configured 2 OCPU / 12 GB compute budget.
- The configured region must match the configured home region.
- Larger shapes or volumes are rejected by the program.

The configured compartment should be dedicated to this deployment or contain no other A1 instances. Account-wide compute usage in other compartments is not enumerated by the compute-budget check. The regional free-storage check uses OCI's resource-availability API.

Capacity failures, throttling, transient server errors, and transport failures are retried. Authentication, authorization, configuration, and resource mismatch failures stop the job for investigation. The workflow does not automatically change regions, upgrade the account, or fall back to a paid shape.

Always Free eligibility, capacity, and billing remain subject to Oracle's current terms. An IAM quota can provide additional limits independently of this program.

## Authentication

The workflow uses a dedicated API-only OCI user. An interactive security token is unsuitable because it expires. Credentials are stored in GitHub Actions secrets and the private signing key is passed to the OCI SDK in memory.

Required repository secrets:

| Secret | Purpose |
| --- | --- |
| `OCI_USER_OCID` | Dedicated automation user |
| `OCI_FINGERPRINT` | API signing key fingerprint |
| `OCI_PRIVATE_KEY` | PEM private signing key |
| `OCI_TENANCY_OCID` | Tenancy |
| `OCI_COMPARTMENT_OCID` | Resource compartment |
| `OCI_SUBNET_OCID` | Existing public subnet |
| `OCI_AVAILABILITY_DOMAIN` | Home-region availability domain |
| `OCI_IMAGE_OCID` | Available Ubuntu 24.04 ARM64 platform image |
| `OCI_SSH_PUBLIC_KEY` | Public recovery key; never provide its private key |

Required repository variables:

| Variable | Purpose |
| --- | --- |
| `OCI_REGION` | Target region |
| `OCI_HOME_REGION` | Actual tenancy home region |

The subnet, route table, internet gateway, and ingress rules must already exist. The provisioner does not modify them.

The automation user has infrastructure read permissions and creation-related operations for Compute, Block Volume, and reserved public IPs. Its policy excludes delete operations and IAM administration. Console passwords and non-API credentials are disabled. Policy templates are in `iam-policy-statements.txt`; replace the group name if necessary.

Pull-request tests receive no OCI secrets and cannot trigger provisioning. Official GitHub actions are pinned to commit hashes, and the OCI SDK is pinned in `requirements.txt`.

## Guest bootstrap

`provision.py` builds cloud-init metadata that:

- Installs the resource controller and its systemd unit.
- Enables the controller on boot with automatic restart.
- Disables SSH password authentication and direct root login.
- Prepares the local sudoers entry needed for Oracle Cloud Agent Run Command.

Run Command still requires an appropriate OCI dynamic group and execution policy. Cloudflare Tunnel and Access are not configured by this workflow.

The controller targets aggregate guest CPU usage and conservatively measured resident-memory usage, rather than adding a fixed load regardless of application activity. It releases ballast under memory pressure and reduces CPU duty when other processes use resources. Its systemd unit uses low scheduling priority, an aggregate CPU quota, memory limits, no swap, and an unprivileged dynamic user.

Guest measurements do not prove Oracle's reclamation criteria are satisfied. After provisioning, verify cloud-init completion, the running service, and Oracle-side CPU and memory metrics. No seven-day observation or permanent non-reclamation guarantee is implied.

## Local verification

The standard-library tests do not need credentials. The in-memory authentication integration test uses an ephemeral test key and runs when the OCI SDK is installed; both GitHub workflows install the SDK before testing:

```bash
python3 -m unittest discover -v
systemd-analyze verify resource-guard.service
```

For authenticated verification, install the pinned dependency in a virtual environment and provide the environment variables listed above. `OCI_CONFIG_FILE` can point to a protected local OCI API-key configuration instead of passing the signing key through the environment.

```bash
python provision.py --preflight-only
python provision.py --once
python provision.py --window-seconds 3600
```

`--preflight-only` performs read-only checks. `--once` can create real resources and performs one reconciliation attempt. The default invocation retries for one hour. Results are written to the ignored `output/` directory.
