"""Idempotent provisioning of one home-region Always Free A1 instance."""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime
import json
import os
import sys
import time
import uuid
from pathlib import Path


class FatalError(Exception):
    pass


class RetryableError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Config:
    tenancy_id: str
    compartment_id: str
    subnet_id: str
    availability_domain: str
    image_id: str
    ssh_public_key: str
    region: str
    home_region: str
    name: str = "ubuntu-a1"
    ocpus: int = 2
    memory_gb: int = 12
    boot_gb: int = 50
    data_gb: int = 100

    def __post_init__(self):
        if self.region != self.home_region:
            raise ValueError("Always Free provisioning must use the home region")
        if (self.ocpus, self.memory_gb, self.boot_gb, self.data_gb) != (2, 12, 50, 100):
            raise ValueError("Only the requested 2 OCPU, 12 GB RAM, and 150 GB total storage configuration is permitted")
        if not all((self.tenancy_id, self.compartment_id, self.subnet_id, self.availability_domain, self.image_id)):
            raise ValueError("Required OCI identifiers are missing")
        if not self.ssh_public_key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
            raise ValueError("A public SSH recovery key is required")

    def token(self, resource: str) -> str:
        identity = f"oci-always-free:{self.compartment_id}:{self.subnet_id}:{self.name}:{resource}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))

    @property
    def tags(self):
        return {"managed-by": "github-actions-a1", "provisioning-key": self.token("deployment")}

    @classmethod
    def from_environment(cls):
        def required(name):
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(f"Missing environment variable: {name}")
            return value

        tenancy = required("OCI_TENANCY_OCID")
        return cls(
            tenancy_id=tenancy,
            compartment_id=os.environ.get("OCI_COMPARTMENT_OCID", tenancy),
            subnet_id=required("OCI_SUBNET_OCID"),
            availability_domain=required("OCI_AVAILABILITY_DOMAIN"),
            image_id=required("OCI_IMAGE_OCID"),
            ssh_public_key=required("OCI_SSH_PUBLIC_KEY"),
            region=required("OCI_REGION"),
            home_region=required("OCI_HOME_REGION"),
            name=os.environ.get("OCI_INSTANCE_NAME", "ubuntu-a1"),
        )


@dataclasses.dataclass(frozen=True)
class Result:
    ready: bool
    message: str
    instance_id: str | None = None
    volume_id: str | None = None
    public_ip: str | None = None


def provision_once(backend, cfg: Config) -> Result:
    instance = backend.find_instance(cfg)
    if instance is None:
        used_ocpus, used_memory = backend.a1_usage(cfg)
        if used_ocpus + cfg.ocpus > 2 or used_memory + cfg.memory_gb > 12:
            raise FatalError("Other A1 instances already consume the configured Always Free compute budget")
        if backend.free_storage_gb(cfg) < cfg.boot_gb + cfg.data_gb:
            raise FatalError("Insufficient unused Always Free storage for the complete 150 GB configuration")
        instance = backend.launch_instance(cfg)
    if instance["state"] in {"TERMINATING", "TERMINATED", "STOPPED", "STOPPING"}:
        raise FatalError("The managed instance is stopped or terminated; refusing automatic replacement")
    if (instance["shape"], instance["ocpus"], instance["memory_gb"]) != ("VM.Standard.A1.Flex", 2, 12):
        raise FatalError("The existing instance does not match the requested configuration")
    if instance["state"] != "RUNNING":
        return Result(False, "Waiting for the instance to become RUNNING", instance["id"])
    boot_size = backend.boot_volume_gb(cfg, instance)
    if boot_size is None:
        return Result(False, "Waiting for the boot volume attachment", instance["id"])
    if boot_size != cfg.boot_gb:
        raise FatalError("Unexpected boot volume size; refusing additional allocations")

    volume = backend.find_volume(cfg)
    if volume is None:
        if backend.free_storage_gb(cfg) < cfg.data_gb:
            raise FatalError("Insufficient unused Always Free storage for the data volume")
        volume = backend.create_volume(cfg)
    if volume["size_gb"] != cfg.data_gb:
        raise FatalError("The existing data volume has an unexpected size")
    if volume["state"] in {"TERMINATING", "TERMINATED", "FAULTY"}:
        raise FatalError("The managed data volume is not usable; refusing automatic replacement")
    if volume["state"] != "AVAILABLE":
        return Result(False, "Waiting for the data volume", instance["id"], volume["id"])
    attachment = backend.find_attachment(cfg, instance, volume)
    if attachment is None:
        attachment = backend.attach_volume(cfg, instance, volume)
    if attachment["state"] != "ATTACHED":
        return Result(False, "Waiting for the data volume attachment", instance["id"], volume["id"])

    private_ip = backend.primary_private_ip(cfg, instance)
    if private_ip is None:
        return Result(False, "Waiting for the primary VNIC", instance["id"], volume["id"])
    public_ip = backend.find_public_ip(cfg)
    if public_ip is None:
        public_ip = backend.create_public_ip(cfg, private_ip)
    elif public_ip["private_ip_id"] not in {None, private_ip}:
        raise FatalError("The reserved public IP belongs to another resource; refusing to move it")
    elif public_ip["private_ip_id"] is None:
        public_ip = backend.assign_public_ip(public_ip, private_ip)
    if public_ip["state"] != "ASSIGNED":
        return Result(False, "Waiting for the reserved IPv4 assignment", instance["id"], volume["id"])
    return Result(True, "Instance RUNNING with 150 GB allocated and a reserved IPv4", instance["id"], volume["id"], public_ip["address"])


def run_retry_loop(attempt, *, window_seconds=3600, retry_seconds=60, clock=time.monotonic, sleep=time.sleep, log=print):
    if window_seconds <= 0 or retry_seconds < 60:
        raise ValueError("The retry window must be positive and retries must be at least 60 seconds apart")
    deadline = clock() + window_seconds
    result = Result(False, "No host capacity during this retry window")
    number = 0
    while clock() < deadline:
        started = clock()
        number += 1
        try:
            result = attempt()
            log(json.dumps({"attempt": number, "ready": result.ready, "message": result.message}, sort_keys=True))
            if result.ready:
                return result
        except RetryableError as exc:
            log(json.dumps({"attempt": number, "ready": False, "message": str(exc)}, sort_keys=True))
        delay = min(deadline - clock(), max(0, started + retry_seconds - clock()))
        if delay > 0:
            sleep(delay)
    return result


def build_user_data() -> str:
    root = Path(__file__).resolve().parent
    files = [
        {"path": "/opt/resource-guard/resource_guard.py", "owner": "root:root", "permissions": "0644", "content": (root / "resource_guard.py").read_text()},
        {"path": "/etc/systemd/system/resource-guard.service", "owner": "root:root", "permissions": "0644", "content": (root / "resource-guard.service").read_text()},
        {"path": "/etc/sudoers.d/101-oracle-cloud-agent-run-command", "owner": "root:root", "permissions": "0440", "content": "ocarun ALL=(ALL) NOPASSWD:ALL\n"},
        {"path": "/etc/ssh/sshd_config.d/00-admin-access.conf", "owner": "root:root", "permissions": "0644", "content": "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n"},
    ]
    document = {
        "hostname": "ubuntu-a1", "manage_etc_hosts": True, "ssh_pwauth": False, "disable_root": True,
        "write_files": files,
        "runcmd": [
            ["/usr/sbin/visudo", "-cf", "/etc/sudoers.d/101-oracle-cloud-agent-run-command"],
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable", "--now", "resource-guard.service"],
            ["systemctl", "reload", "ssh"],
        ],
    }
    return base64.b64encode(("#cloud-config\n" + json.dumps(document)).encode()).decode()


class OciBackend:
    def __init__(self, cfg: Config):
        import oci

        self.oci = oci
        if os.environ.get("OCI_CONFIG_FILE"):
            auth = oci.config.from_file(os.environ["OCI_CONFIG_FILE"])
            signer = None
        else:
            auth = {"region": cfg.region}
            signer = oci.signer.Signer(
                tenancy=cfg.tenancy_id, user=os.environ["OCI_USER_OCID"],
                fingerprint=os.environ["OCI_FINGERPRINT"], private_key_file_location=None,
                private_key_content=os.environ["OCI_PRIVATE_KEY"],
            )
        options = {"retry_strategy": oci.retry.NoneRetryStrategy(), "timeout": (10, 30)}
        if signer is not None:
            options["signer"] = signer
        self.compute = oci.core.ComputeClient(auth, **options)
        self.block = oci.core.BlockstorageClient(auth, **options)
        self.network = oci.core.VirtualNetworkClient(auth, **options)
        self.limits = oci.limits.LimitsClient(auth, **options)

    def call(self, operation, *args, **kwargs):
        from oci._vendor import requests

        try:
            return operation(*args, **kwargs)
        except self.oci.exceptions.ServiceError as exc:
            if exc.status in {409, 429} or exc.status >= 500:
                reason = "Out of host capacity" if "out of host capacity" in str(exc.message).lower() else f"Transient OCI response: status={exc.status}, code={exc.code}"
                raise RetryableError(reason) from exc
            raise FatalError(f"OCI operation failed: status={exc.status}, code={exc.code}; check credentials and policy") from exc
        except requests.exceptions.RequestException as exc:
            raise RetryableError("OCI transport failure; checking existing resources before retrying") from exc

    def listed(self, operation, **kwargs):
        response = self.call(self.oci.pagination.list_call_get_all_results, operation, **kwargs)
        return response.data

    @staticmethod
    def only_managed(items, cfg, description):
        selected = [item for item in items if all((item.freeform_tags or {}).get(key) == value for key, value in cfg.tags.items())]
        if len(selected) > 1:
            raise FatalError(f"Multiple managed {description} resources exist; manual review is required")
        return selected[0] if selected else None

    def preflight(self, cfg):
        image = self.call(self.compute.get_image, cfg.image_id).data
        if image.lifecycle_state != "AVAILABLE" or image.operating_system != "Canonical Ubuntu" or image.operating_system_version != "24.04" or "aarch64" not in image.display_name:
            raise FatalError("The selected image must be an available Ubuntu 24.04 ARM64 platform image")
        subnet = self.call(self.network.get_subnet, cfg.subnet_id).data
        if subnet.lifecycle_state != "AVAILABLE" or subnet.prohibit_public_ip_on_vnic:
            raise FatalError("The existing subnet must support public IPv4 assignment")

    def instances(self, cfg):
        return self.listed(self.compute.list_instances, compartment_id=cfg.compartment_id)

    def find_instance(self, cfg):
        items = self.instances(cfg)
        item = self.only_managed(items, cfg, "instance")
        if item is None:
            if any(item.display_name == cfg.name and item.lifecycle_state != "TERMINATED" for item in items):
                raise FatalError("An unmanaged instance already uses the requested name")
            return None
        return self.instance_dict(item)

    @staticmethod
    def instance_dict(item):
        shape = item.shape_config
        return {"id": item.id, "state": item.lifecycle_state, "shape": item.shape, "ocpus": shape.ocpus if shape else None, "memory_gb": shape.memory_in_gbs if shape else None}

    def a1_usage(self, cfg):
        active = [item for item in self.instances(cfg) if item.shape == "VM.Standard.A1.Flex" and item.lifecycle_state != "TERMINATED"]
        return sum(item.shape_config.ocpus for item in active), sum(item.shape_config.memory_in_gbs for item in active)

    def free_storage_gb(self, cfg):
        return self.call(self.limits.get_resource_availability, "block-storage", "total-free-storage-gb-regional", cfg.compartment_id).data.available

    def launch_instance(self, cfg):
        models = self.oci.core.models
        details = models.LaunchInstanceDetails(
            compartment_id=cfg.compartment_id, availability_domain=cfg.availability_domain,
            display_name=cfg.name, shape="VM.Standard.A1.Flex",
            shape_config=models.LaunchInstanceShapeConfigDetails(ocpus=cfg.ocpus, memory_in_gbs=cfg.memory_gb),
            source_details=models.InstanceSourceViaImageDetails(image_id=cfg.image_id, boot_volume_size_in_gbs=cfg.boot_gb, boot_volume_vpus_per_gb=10),
            create_vnic_details=models.CreateVnicDetails(subnet_id=cfg.subnet_id, assign_public_ip=False, hostname_label="ubuntu-a1"),
            agent_config=models.LaunchInstanceAgentConfigDetails(
                are_all_plugins_disabled=False, is_monitoring_disabled=False, is_management_disabled=False,
                plugins_config=[models.InstanceAgentPluginConfigDetails(name=name, desired_state="ENABLED") for name in ["Compute Instance Monitoring", "Compute Instance Run Command"]],
            ),
            instance_options=models.InstanceOptions(are_legacy_imds_endpoints_disabled=True),
            metadata={"ssh_authorized_keys": cfg.ssh_public_key, "user_data": build_user_data()},
            freeform_tags=cfg.tags,
        )
        response = self.call(self.compute.launch_instance, details, opc_retry_token=cfg.token("instance"))
        return self.instance_dict(response.data)

    def boot_volume_gb(self, cfg, instance):
        attachments = self.listed(self.compute.list_boot_volume_attachments, availability_domain=cfg.availability_domain, compartment_id=cfg.compartment_id, instance_id=instance["id"])
        attached = [item for item in attachments if item.lifecycle_state == "ATTACHED"]
        if not attached:
            return None
        return self.call(self.block.get_boot_volume, attached[0].boot_volume_id).data.size_in_gbs

    def find_volume(self, cfg):
        volumes = self.listed(self.block.list_volumes, compartment_id=cfg.compartment_id, availability_domain=cfg.availability_domain)
        volume = self.only_managed(volumes, cfg, "data volume")
        return self.volume_dict(volume) if volume else None

    @staticmethod
    def volume_dict(volume):
        return {"id": volume.id, "state": volume.lifecycle_state, "size_gb": volume.size_in_gbs}

    def create_volume(self, cfg):
        details = self.oci.core.models.CreateVolumeDetails(
            compartment_id=cfg.compartment_id, availability_domain=cfg.availability_domain,
            display_name=f"{cfg.name}-data", size_in_gbs=cfg.data_gb, vpus_per_gb=10,
            is_auto_tune_enabled=False, freeform_tags=cfg.tags,
        )
        return self.volume_dict(self.call(self.block.create_volume, details, opc_retry_token=cfg.token("volume")).data)

    def find_attachment(self, cfg, instance, volume):
        items = self.listed(self.compute.list_volume_attachments, compartment_id=cfg.compartment_id, instance_id=instance["id"])
        active = [item for item in items if item.volume_id == volume["id"] and item.lifecycle_state != "DETACHED"]
        if len(active) > 1:
            raise FatalError("Multiple active data volume attachments exist")
        return {"id": active[0].id, "state": active[0].lifecycle_state} if active else None

    def attach_volume(self, cfg, instance, volume):
        details = self.oci.core.models.AttachParavirtualizedVolumeDetails(
            instance_id=instance["id"], volume_id=volume["id"], display_name=f"{cfg.name}-data",
            is_read_only=False, is_shareable=False,
        )
        item = self.call(self.compute.attach_volume, details).data
        return {"id": item.id, "state": item.lifecycle_state}

    def primary_private_ip(self, cfg, instance):
        attachments = self.listed(self.compute.list_vnic_attachments, compartment_id=cfg.compartment_id, instance_id=instance["id"])
        for attachment in attachments:
            if attachment.lifecycle_state != "ATTACHED":
                continue
            vnic = self.call(self.network.get_vnic, attachment.vnic_id).data
            if vnic.is_primary:
                private_ips = self.listed(self.network.list_private_ips, vnic_id=vnic.id)
                return next((item.id for item in private_ips if item.is_primary), None)
        return None

    @staticmethod
    def public_ip_dict(item):
        return {"id": item.id, "state": item.lifecycle_state, "private_ip_id": item.assigned_entity_id or item.private_ip_id, "address": item.ip_address}

    def find_public_ip(self, cfg):
        items = self.listed(self.network.list_public_ips, scope="REGION", compartment_id=cfg.compartment_id)
        item = self.only_managed(items, cfg, "reserved public IP")
        return self.public_ip_dict(item) if item else None

    def create_public_ip(self, cfg, private_ip):
        details = self.oci.core.models.CreatePublicIpDetails(compartment_id=cfg.compartment_id, lifetime="RESERVED", private_ip_id=private_ip, display_name=f"{cfg.name}-ipv4", freeform_tags=cfg.tags)
        return self.public_ip_dict(self.call(self.network.create_public_ip, details).data)

    def assign_public_ip(self, public_ip, private_ip):
        details = self.oci.core.models.UpdatePublicIpDetails(private_ip_id=private_ip)
        return self.public_ip_dict(self.call(self.network.update_public_ip, public_ip["id"], details).data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-seconds", type=int, default=3600)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.window_seconds <= 3600:
        parser.error("The retry window must be between 1 and 3600 seconds")
    try:
        cfg = Config.from_environment()
        backend = OciBackend(cfg)
        if args.preflight_only:
            backend.preflight(cfg)
            print(json.dumps({"preflight": "ok", "free_storage_gb": backend.free_storage_gb(cfg), "a1_usage": backend.a1_usage(cfg)}))
            return 0
        validated = False

        def attempt():
            nonlocal validated
            if not validated:
                backend.preflight(cfg)
                validated = True
            return provision_once(backend, cfg)

        if args.once:
            try:
                result = attempt()
            except RetryableError as exc:
                result = Result(False, str(exc))
        else:
            result = run_retry_loop(attempt, window_seconds=args.window_seconds)
        report = dataclasses.asdict(result)
        report["checked_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        report["data_volume_note"] = "Attached block storage; filesystem initialization and mounting are a separate step."
        directory = Path("output")
        directory.mkdir(exist_ok=True)
        (directory / "provisioning-result.json").write_text(json.dumps(report, indent=2) + "\n")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write(f"ready={str(result.ready).lower()}\n")
        print(json.dumps(report, sort_keys=True))
        return 0
    except (FatalError, ValueError, KeyError) as exc:
        print(f"Provisioning stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
