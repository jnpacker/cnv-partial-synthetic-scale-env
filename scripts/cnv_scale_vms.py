#!/usr/bin/env python3
"""
CNV Scale Test VM Management Script

Creates and manages test VirtualMachine resources for CNV performance testing.
VMs are created with randomized specs but never started - used for console/performance measurement.
VMs are automatically distributed across multiple namespaces for realistic multi-tenant testing.
"""

import argparse
import random
import string
import sys
import time
import warnings
from datetime import datetime
from typing import Dict, List, Tuple

# Show InsecureRequestWarning only once
warnings.filterwarnings('once', message='Unverified HTTPS request')

try:
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException
except ImportError:
    print("Error: kubernetes Python client is required. Install with: pip install kubernetes")
    sys.exit(1)

try:
    from tqdm import tqdm
except ImportError:
    print("Error: tqdm is required for progress bars. Install with: pip install tqdm")
    sys.exit(1)


# Configuration
DEFAULT_VM_COUNT = 500
MAX_VM_COUNT = 999
MAX_NAMESPACES = 999
VM_LABEL_KEY = "cnv-scale-test"
VM_LABEL_VALUE = "synthetic-workload"
NAMESPACE_LABEL_KEY = "cnv-scale-test"
NAMESPACE_LABEL_VALUE = "synthetic-workload"
VM_PREFIX = "qe-virt"
NAMESPACE_PREFIX = "qe-ns"

# Randomization ranges
CPU_RANGE = (1, 4)  # cores
MEMORY_RANGE = (1, 8)  # GiB
DISK_RANGE = (10, 50)  # GiB
VMS_PER_NAMESPACE_RANGE = (1, 20)  # VMs per namespace


def generate_random_suffix(length: int = 5) -> str:
    """Generate a random alphanumeric suffix."""
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))


def generate_namespace_name(index: int) -> str:
    """Generate namespace name in format: qe-ns-001"""
    return f"{NAMESPACE_PREFIX}-{index:03d}"


def generate_vm_name(index: int) -> str:
    """Generate VM name in format: qe-virt-001-xxxxx"""
    suffix = generate_random_suffix()
    return f"{VM_PREFIX}-{index:03d}-{suffix}"


def generate_random_specs() -> Dict[str, any]:
    """Generate random CPU, memory, and disk specifications."""
    cpu_cores = random.randint(*CPU_RANGE)
    memory_gi = random.randint(*MEMORY_RANGE)
    disk_gi = random.randint(*DISK_RANGE)

    return {
        "cpu": cpu_cores,
        "memory": f"{memory_gi}Gi",
        "disk": f"{disk_gi}Gi"
    }


def get_or_create_namespace(v1: client.CoreV1Api, namespace_name: str, dry_run: bool = False) -> Tuple[bool, str]:
    """
    Get or create a namespace with the test label.

    Returns:
        Tuple of (created, status_message)
        created: True if namespace was created, False if it existed
    """
    try:
        # Try to get the namespace
        ns = v1.read_namespace(name=namespace_name)
        # Check if it has our label
        labels = ns.metadata.labels or {}
        if labels.get(NAMESPACE_LABEL_KEY) == NAMESPACE_LABEL_VALUE:
            return False, f"Reusing existing namespace: {namespace_name}"
        else:
            return False, f"Namespace {namespace_name} exists but lacks label, reusing anyway"
    except ApiException as e:
        if e.status == 404:
            # Namespace doesn't exist, create it
            if dry_run:
                return True, f"[DRY RUN] Would create namespace: {namespace_name}"

            namespace = client.V1Namespace(
                metadata=client.V1ObjectMeta(
                    name=namespace_name,
                    labels={
                        NAMESPACE_LABEL_KEY: NAMESPACE_LABEL_VALUE
                    }
                )
            )
            try:
                v1.create_namespace(body=namespace)
                return True, f"Created namespace: {namespace_name}"
            except ApiException as create_error:
                return False, f"Error creating namespace {namespace_name}: {create_error.reason}"
        else:
            return False, f"Error checking namespace {namespace_name}: {e.reason}"


def delete_namespace_if_empty(v1: client.CoreV1Api, custom_api: client.CustomObjectsApi,
                               namespace_name: str, dry_run: bool = False) -> Tuple[bool, str]:
    """
    Delete a namespace if it only contains our test VMs and they're all gone.

    Returns:
        Tuple of (deleted, status_message)
    """
    try:
        # Check if namespace has our label
        ns = v1.read_namespace(name=namespace_name)
        labels = ns.metadata.labels or {}
        if labels.get(NAMESPACE_LABEL_KEY) != NAMESPACE_LABEL_VALUE:
            return False, f"Namespace {namespace_name} doesn't have our label, skipping"

        # Check for any remaining VMs with our label
        vms = custom_api.list_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace_name,
            plural="virtualmachines",
            label_selector=f"{VM_LABEL_KEY}={VM_LABEL_VALUE}"
        )

        if len(vms.get("items", [])) > 0:
            return False, f"Namespace {namespace_name} still has VMs, skipping deletion"

        # Check for any other VMs (not created by us)
        all_vms = custom_api.list_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace_name,
            plural="virtualmachines"
        )

        if len(all_vms.get("items", [])) > 0:
            return False, f"Namespace {namespace_name} has other VMs, skipping deletion"

        # Safe to delete
        if dry_run:
            return True, f"[DRY RUN] Would delete namespace: {namespace_name}"

        v1.delete_namespace(name=namespace_name)
        return True, f"Deleted namespace: {namespace_name}"

    except ApiException as e:
        if e.status == 404:
            return False, f"Namespace {namespace_name} not found"
        return False, f"Error checking namespace {namespace_name}: {e.reason}"
    except Exception as e:
        return False, f"Error with namespace {namespace_name}: {str(e)}"


def create_vm_manifest(name: str, namespace: str, specs: Dict[str, any]) -> Dict:
    """
    Create a VirtualMachine manifest with sparse disk allocation.

    The VM uses:
    - containerdisk with cirros image (lightweight)
    - sparse allocation (allocate on use, not pre-allocated)
    - randomized CPU, memory, and disk
    """
    manifest = {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachine",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                VM_LABEL_KEY: VM_LABEL_VALUE,
                "vm-index": name.split('-')[2]  # Extract the numeric index
            }
        },
        "spec": {
            "running": False,  # Never start these VMs
            "template": {
                "metadata": {
                    "labels": {
                        "kubevirt.io/vm": name,
                        VM_LABEL_KEY: VM_LABEL_VALUE
                    }
                },
                "spec": {
                    "domain": {
                        "cpu": {
                            "cores": specs["cpu"]
                        },
                        "resources": {
                            "requests": {
                                "memory": specs["memory"]
                            }
                        },
                        "devices": {
                            "disks": [
                                {
                                    "name": "containerdisk",
                                    "disk": {
                                        "bus": "virtio"
                                    }
                                },
                                {
                                    "name": "emptydisk",
                                    "disk": {
                                        "bus": "virtio"
                                    }
                                }
                            ]
                        }
                    },
                    "volumes": [
                        {
                            "name": "containerdisk",
                            "containerDisk": {
                                "image": "quay.io/kubevirt/cirros-container-disk-demo"
                            }
                        },
                        {
                            "name": "emptydisk",
                            "emptyDisk": {
                                "capacity": specs["disk"]
                            }
                        }
                    ]
                }
            }
        }
    }

    return manifest


def create_vms(count: int, dry_run: bool = False) -> Tuple[int, List[Dict], Dict]:
    """
    Create the specified number of VirtualMachine resources distributed across namespaces.

    Returns:
        Tuple of (successful_count, list of created VM details, namespace stats)
    """
    if count < 1:
        raise ValueError(f"VM count must be at least 1")

    # Load kubeconfig
    try:
        config.load_kube_config()
    except Exception as e:
        print(f"Error loading kubeconfig: {e}")
        sys.exit(1)

    # Create API clients
    v1 = client.CoreV1Api()
    custom_api = client.CustomObjectsApi()

    created_vms = []
    failed_vms = []
    namespace_stats = {}
    created_namespaces = []
    reused_namespaces = []
    start_time = datetime.now()

    print(f"\n{'=' * 80}")
    print(f"Starting VM creation: {count} VirtualMachines")
    print(f"Distribution: {VMS_PER_NAMESPACE_RANGE[0]}-{VMS_PER_NAMESPACE_RANGE[1]} VMs per namespace")
    print(f"VM Label: {VM_LABEL_KEY}={VM_LABEL_VALUE}")
    print(f"Namespace Label: {NAMESPACE_LABEL_KEY}={NAMESPACE_LABEL_VALUE}")
    print(f"Dry run: {dry_run}")
    print(f"{'=' * 80}\n")

    # Calculate namespace distribution
    remaining_vms = count
    namespace_index = 1
    vm_index = 1

    # Create progress bar
    pbar = tqdm(total=count, desc="Creating VMs", unit="VM", ncols=100)

    while remaining_vms > 0 and namespace_index <= MAX_NAMESPACES:
        # Determine how many VMs for this namespace
        max_for_this_ns = min(remaining_vms, VMS_PER_NAMESPACE_RANGE[1])
        min_for_this_ns = min(VMS_PER_NAMESPACE_RANGE[0], max_for_this_ns)
        vms_in_namespace = random.randint(min_for_this_ns, max_for_this_ns)

        namespace_name = generate_namespace_name(namespace_index)

        # Create or get namespace
        ns_created, ns_message = get_or_create_namespace(v1, namespace_name, dry_run)
        if not dry_run:
            if ns_created:
                created_namespaces.append(namespace_name)
            else:
                reused_namespaces.append(namespace_name)

        if ns_created or "Reusing" in ns_message:
            namespace_stats[namespace_name] = {
                "created": ns_created,
                "vm_count": 0,
                "failed_count": 0
            }

            # Create VMs in this namespace
            for _ in range(vms_in_namespace):
                if vm_index > count:
                    break

                vm_name = generate_vm_name(vm_index)
                specs = generate_random_specs()

                try:
                    manifest = create_vm_manifest(vm_name, namespace_name, specs)

                    if dry_run:
                        created_vms.append({
                            "name": vm_name,
                            "namespace": namespace_name,
                            "specs": specs,
                            "status": "dry-run"
                        })
                        namespace_stats[namespace_name]["vm_count"] += 1
                    else:
                        # Create the VM
                        custom_api.create_namespaced_custom_object(
                            group="kubevirt.io",
                            version="v1",
                            namespace=namespace_name,
                            plural="virtualmachines",
                            body=manifest
                        )

                        created_vms.append({
                            "name": vm_name,
                            "namespace": namespace_name,
                            "specs": specs,
                            "status": "created"
                        })
                        namespace_stats[namespace_name]["vm_count"] += 1

                    # Update progress bar
                    pbar.update(1)
                    pbar.set_postfix({"Namespaces": len(namespace_stats), "Failed": len(failed_vms)})

                except ApiException as e:
                    failed_vms.append({
                        "name": vm_name,
                        "namespace": namespace_name,
                        "error": str(e.reason)
                    })
                    namespace_stats[namespace_name]["failed_count"] += 1
                    pbar.update(1)
                    pbar.set_postfix({"Namespaces": len(namespace_stats), "Failed": len(failed_vms)})
                except Exception as e:
                    failed_vms.append({
                        "name": vm_name,
                        "namespace": namespace_name,
                        "error": str(e)
                    })
                    namespace_stats[namespace_name]["failed_count"] += 1
                    pbar.update(1)
                    pbar.set_postfix({"Namespaces": len(namespace_stats), "Failed": len(failed_vms)})

                vm_index += 1
                remaining_vms -= 1

        namespace_index += 1

    # Close progress bar
    pbar.close()

    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    # Summary
    print(f"\n{'=' * 80}")
    print(f"VM Creation Summary")
    print(f"{'=' * 80}")
    print(f"Total requested: {count}")
    print(f"Successfully created: {len(created_vms)}")
    print(f"Failed: {len(failed_vms)}")
    print(f"Duration: {duration:.2f} seconds")
    if len(created_vms) > 0:
        print(f"Average: {duration/len(created_vms):.2f} seconds per VM")

    print(f"\nNamespace Statistics:")
    print(f"  Total namespaces used: {len(namespace_stats)}")
    print(f"  Newly created: {len(created_namespaces)}")
    print(f"  Reused existing: {len(reused_namespaces)}")

    # Statistics on randomized specs
    if created_vms:
        cpu_counts = [vm["specs"]["cpu"] for vm in created_vms]
        memory_sizes = [int(vm["specs"]["memory"].rstrip("Gi")) for vm in created_vms]
        disk_sizes = [int(vm["specs"]["disk"].rstrip("Gi")) for vm in created_vms]

        print(f"\nRandomized Specifications:")
        print(f"  CPU cores:   min={min(cpu_counts)}, max={max(cpu_counts)}, avg={sum(cpu_counts)/len(cpu_counts):.1f}")
        print(f"  Memory (Gi): min={min(memory_sizes)}, max={max(memory_sizes)}, avg={sum(memory_sizes)/len(memory_sizes):.1f}")
        print(f"  Disk (Gi):   min={min(disk_sizes)}, max={max(disk_sizes)}, avg={sum(disk_sizes)/len(disk_sizes):.1f}")

        # VM distribution per namespace
        vms_per_ns = [stats["vm_count"] for stats in namespace_stats.values()]
        print(f"\nVMs per Namespace:")
        print(f"  min={min(vms_per_ns)}, max={max(vms_per_ns)}, avg={sum(vms_per_ns)/len(vms_per_ns):.1f}")

    if failed_vms:
        print(f"\nFailed VMs:")
        for vm in failed_vms[:10]:  # Show first 10 failures
            print(f"  - {vm['name']} in {vm['namespace']}: {vm['error']}")
        if len(failed_vms) > 10:
            print(f"  ... and {len(failed_vms) - 10} more")

    print(f"{'=' * 80}\n")

    return len(created_vms), created_vms, namespace_stats


def delete_vms(dry_run: bool = False) -> Tuple[int, int]:
    """
    Delete all VirtualMachine resources with the test label across all namespaces.
    Then delete empty namespaces that have our label.

    Returns:
        Tuple of (vms_deleted, namespaces_deleted)
    """
    # Load kubeconfig
    try:
        config.load_kube_config()
    except Exception as e:
        print(f"Error loading kubeconfig: {e}")
        sys.exit(1)

    # Create API clients
    v1 = client.CoreV1Api()
    custom_api = client.CustomObjectsApi()

    start_time = datetime.now()

    print(f"\n{'=' * 80}")
    print(f"Starting VM deletion across all namespaces")
    print(f"VM Label selector: {VM_LABEL_KEY}={VM_LABEL_VALUE}")
    print(f"Namespace Label: {NAMESPACE_LABEL_KEY}={NAMESPACE_LABEL_VALUE}")
    print(f"Dry run: {dry_run}")
    print(f"{'=' * 80}\n")

    # First, find all namespaces with our label
    try:
        namespaces = v1.list_namespace(
            label_selector=f"{NAMESPACE_LABEL_KEY}={NAMESPACE_LABEL_VALUE}"
        )
        namespace_list = [ns.metadata.name for ns in namespaces.items]

        if not namespace_list:
            print(f"No namespaces found with label {NAMESPACE_LABEL_KEY}={NAMESPACE_LABEL_VALUE}")
            print("No VMs to delete.")
            return 0, 0

        print(f"Found {len(namespace_list)} namespaces with label:")
        for ns in namespace_list:
            print(f"  - {ns}")
        print()

    except ApiException as e:
        print(f"Error listing namespaces: {e.reason}")
        return 0, 0
    except Exception as e:
        print(f"Unexpected error: {e}")
        return 0, 0

    # First, count total VMs to delete
    total_vms_to_delete = 0
    namespace_vm_counts = {}

    for namespace in namespace_list:
        try:
            vms = custom_api.list_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachines",
                label_selector=f"{VM_LABEL_KEY}={VM_LABEL_VALUE}"
            )
            vm_count = len(vms.get("items", []))
            namespace_vm_counts[namespace] = vm_count
            total_vms_to_delete += vm_count
        except:
            pass

    if total_vms_to_delete == 0:
        print("No VMs found to delete.\n")
        return 0, 0

    print(f"Found {total_vms_to_delete} VMs to delete\n")

    # Delete VMs from each namespace
    total_vms_deleted = 0
    total_vms_failed = 0

    # Create progress bar for deletion
    pbar = tqdm(total=total_vms_to_delete, desc="Deleting VMs", unit="VM", ncols=100)

    for namespace in namespace_list:
        try:
            # List VMs with the label in this namespace
            vms = custom_api.list_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachines",
                label_selector=f"{VM_LABEL_KEY}={VM_LABEL_VALUE}"
            )

            vm_list = vms.get("items", [])

            if len(vm_list) == 0:
                continue

            deleted_in_ns = 0
            failed_in_ns = 0

            for vm in vm_list:
                vm_name = vm["metadata"]["name"]

                try:
                    if dry_run:
                        deleted_in_ns += 1
                        total_vms_deleted += 1
                    else:
                        custom_api.delete_namespaced_custom_object(
                            group="kubevirt.io",
                            version="v1",
                            namespace=namespace,
                            plural="virtualmachines",
                            name=vm_name
                        )
                        deleted_in_ns += 1
                        total_vms_deleted += 1

                    # Update progress bar
                    pbar.update(1)
                    pbar.set_postfix({"Namespace": namespace, "Failed": total_vms_failed})

                except ApiException as e:
                    failed_in_ns += 1
                    total_vms_failed += 1
                    pbar.update(1)
                    pbar.set_postfix({"Namespace": namespace, "Failed": total_vms_failed})
                except Exception as e:
                    failed_in_ns += 1
                    total_vms_failed += 1
                    pbar.update(1)
                    pbar.set_postfix({"Namespace": namespace, "Failed": total_vms_failed})

        except ApiException as e:
            print(f"Error listing VMs in {namespace}: {e.reason}")
        except Exception as e:
            print(f"Unexpected error with {namespace}: {e}")

    # Close progress bar
    pbar.close()

    # Now delete empty namespaces
    print(f"\nChecking namespaces for deletion...")
    namespaces_deleted = 0
    namespaces_skipped = 0

    if not dry_run:
        # Wait a bit for VM deletions to propagate
        time.sleep(2)

    for namespace in namespace_list:
        deleted, message = delete_namespace_if_empty(v1, custom_api, namespace, dry_run)
        if deleted:
            namespaces_deleted += 1
            print(message)
        else:
            namespaces_skipped += 1
            if "still has VMs" not in message and "other VMs" not in message:
                print(message)

    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    # Summary
    print(f"\n{'=' * 80}")
    print(f"Deletion Summary")
    print(f"{'=' * 80}")
    print(f"Total VMs deleted: {total_vms_deleted}")
    print(f"Failed VM deletions: {total_vms_failed}")
    print(f"Namespaces deleted: {namespaces_deleted}")
    print(f"Namespaces retained: {namespaces_skipped}")
    print(f"Duration: {duration:.2f} seconds")
    if total_vms_deleted > 0:
        print(f"Average: {duration/total_vms_deleted:.2f} seconds per VM")
    print(f"{'=' * 80}\n")

    return total_vms_deleted, namespaces_deleted


def list_vms() -> None:
    """List all VirtualMachine resources with the test label across all namespaces."""
    try:
        config.load_kube_config()
    except Exception as e:
        print(f"Error loading kubeconfig: {e}")
        sys.exit(1)

    v1 = client.CoreV1Api()
    custom_api = client.CustomObjectsApi()

    print(f"\n{'=' * 80}")
    print(f"VirtualMachines with label {VM_LABEL_KEY}={VM_LABEL_VALUE}")
    print(f"{'=' * 80}\n")

    try:
        # Find all namespaces with our label
        namespaces = v1.list_namespace(
            label_selector=f"{NAMESPACE_LABEL_KEY}={NAMESPACE_LABEL_VALUE}"
        )
        namespace_list = [ns.metadata.name for ns in namespaces.items]

        if not namespace_list:
            print(f"No namespaces found with label {NAMESPACE_LABEL_KEY}={NAMESPACE_LABEL_VALUE}")
            print(f"{'=' * 80}\n")
            return

        total_vms = 0
        namespace_details = {}

        for namespace in namespace_list:
            try:
                vms = custom_api.list_namespaced_custom_object(
                    group="kubevirt.io",
                    version="v1",
                    namespace=namespace,
                    plural="virtualmachines",
                    label_selector=f"{VM_LABEL_KEY}={VM_LABEL_VALUE}"
                )

                vm_list = vms.get("items", [])
                if len(vm_list) > 0:
                    namespace_details[namespace] = vm_list
                    total_vms += len(vm_list)

            except ApiException as e:
                print(f"Error listing VMs in {namespace}: {e.reason}")
            except Exception as e:
                print(f"Unexpected error with {namespace}: {e}")

        print(f"Total: {total_vms} VMs across {len(namespace_details)} namespaces\n")

        for namespace, vm_list in sorted(namespace_details.items()):
            print(f"{namespace}: {len(vm_list)} VMs")
            for vm in vm_list[:5]:  # Show first 5 per namespace
                name = vm["metadata"]["name"]
                running = vm["spec"].get("running", False)
                print(f"  - {name} (running: {running})")
            if len(vm_list) > 5:
                print(f"  ... and {len(vm_list) - 5} more")
            print()

        print(f"{'=' * 80}\n")

    except ApiException as e:
        print(f"Error listing namespaces: {e.reason}")
        print(f"{'=' * 80}\n")
    except Exception as e:
        print(f"Unexpected error: {e}")
        print(f"{'=' * 80}\n")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Create and manage test VirtualMachine resources for CNV performance testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  # Create 500 VMs distributed across namespaces (default)
  {sys.argv[0]} create

  # Create 100 VMs distributed across namespaces
  {sys.argv[0]} create --count 100

  # Delete all test VMs and their namespaces
  {sys.argv[0]} delete

  # List all test VMs across all namespaces
  {sys.argv[0]} list

  # Dry run (no actual creation/deletion)
  {sys.argv[0]} create --dry-run
  {sys.argv[0]} delete --dry-run

Note:
- VMs are created with label {VM_LABEL_KEY}={VM_LABEL_VALUE}
- Namespaces are created with label {NAMESPACE_LABEL_KEY}={NAMESPACE_LABEL_VALUE}
- VMs are distributed randomly (1-20 per namespace) across multiple namespaces
- Namespaces are automatically created (qe-ns-###) and deleted when empty
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Create command
    create_parser = subparsers.add_parser("create", help="Create VirtualMachine resources across namespaces")
    create_parser.add_argument(
        "-c", "--count",
        type=int,
        default=DEFAULT_VM_COUNT,
        help=f"Number of VMs to create (default: {DEFAULT_VM_COUNT})"
    )
    create_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate creation without actually creating resources"
    )

    # Delete command
    delete_parser = subparsers.add_parser("delete", help="Delete VirtualMachine resources and empty namespaces")
    delete_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate deletion without actually deleting resources"
    )

    # List command
    list_parser = subparsers.add_parser("list", help="List VirtualMachine resources across all namespaces")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "create":
        try:
            create_vms(args.count, args.dry_run)
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)
    elif args.command == "delete":
        delete_vms(args.dry_run)
    elif args.command == "list":
        list_vms()


if __name__ == "__main__":
    main()
