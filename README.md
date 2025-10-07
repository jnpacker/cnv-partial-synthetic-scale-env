# CNV Scale Test VM Management

Python script for creating and managing synthetic VirtualMachine resources in CNV (Container Native Virtualization) environments for performance and scale testing.

## Project Structure

```
.
├── README.md                    # This file
├── requirements.txt             # Python dependencies
└── scripts/
    └── cnv_scale_vms.py        # Main VM management script
```

## Features

- **Create up to 999 VMs** with a recommended default of 500
- **Automatic namespace management** - VMs distributed across multiple namespaces
  - Namespaces automatically created (qe-ns-001, qe-ns-002, etc.)
  - Random distribution: 1-20 VMs per namespace
  - Namespace reuse if already exists with proper label
  - Automatic namespace deletion when empty
- **Randomized specifications** for realistic testing:
  - CPU: 1-4 cores
  - Memory: 1-8 GiB
  - Disk: 10-50 GiB (sparse allocation, no pre-allocation)
- **Automatic labeling** for easy identification and bulk operations
- **Detailed statistics** showing creation/deletion progress and spec distribution
- **Dry-run mode** for testing without making changes
- **List, create, and delete** operations with progress tracking

## Prerequisites

- Python 3.6+
- Access to a Kubernetes cluster with CNV/KubeVirt installed
- Valid kubeconfig in your environment
- Appropriate RBAC permissions to create/delete VirtualMachine resources

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Make the script executable:
```bash
chmod +x scripts/cnv_scale_vms.py
```

3. Verify your kubeconfig is configured:
```bash
kubectl get nodes
```

## Usage

### Create VMs

Create 500 VMs distributed across namespaces (default):
```bash
./scripts/cnv_scale_vms.py create
```

Create a specific number of VMs:
```bash
./scripts/cnv_scale_vms.py create --count 100
```

Test without actually creating:
```bash
./scripts/cnv_scale_vms.py create --count 50 --dry-run
```

VMs will be automatically distributed across multiple namespaces (1-20 VMs per namespace randomly)

### List VMs

List all test VMs across all namespaces:
```bash
./scripts/cnv_scale_vms.py list
```

This will show VMs grouped by namespace with totals.

### Delete VMs

Delete all test VMs and empty namespaces:
```bash
./scripts/cnv_scale_vms.py delete
```

Test deletion without actually deleting:
```bash
./scripts/cnv_scale_vms.py delete --dry-run
```

This will:
1. Delete all VMs with the test label from all namespaces
2. Delete any namespaces with the test label that are now empty
3. Preserve namespaces that still contain other resources

## VM Naming Convention

VMs are named using the pattern: `qe-virt-###-xxxxx`

- `qe-virt` - Fixed prefix
- `###` - Sequential number (001-999)
- `xxxxx` - Random 5-character alphanumeric suffix

Example: `qe-virt-001-a7k9m`, `qe-virt-042-p3x8q`

## Labels

All VMs and namespaces are created with labels for easy management:

**VMs:**
```
cnv-scale-test=synthetic-workload
```

**Namespaces:**
```
cnv-scale-test=synthetic-workload
```

These labels are used for:
- Identifying test resources
- Bulk deletion operations
- Filtering in kubectl/oc commands
- Namespace reuse detection

You can also use kubectl/oc directly:
```bash
# List all test namespaces
kubectl get namespaces -l cnv-scale-test=synthetic-workload

# List all test VMs in all namespaces
kubectl get vms -A -l cnv-scale-test=synthetic-workload

# Delete all test VMs in a specific namespace
kubectl delete vms -n qe-ns-001 -l cnv-scale-test=synthetic-workload

# Delete a test namespace (will fail if not empty)
kubectl delete namespace qe-ns-001
```

## Output Example

```
================================================================================
Starting VM creation: 500 VirtualMachines
Distribution: 1-20 VMs per namespace
VM Label: cnv-scale-test=synthetic-workload
Namespace Label: cnv-scale-test=synthetic-workload
Dry run: False
================================================================================

Progress: 10/500 VMs created (2%) across 2 namespaces
Progress: 20/500 VMs created (4%) across 3 namespaces
...
Progress: 500/500 VMs created (100%) across 45 namespaces

================================================================================
VM Creation Summary
================================================================================
Total requested: 500
Successfully created: 500
Failed: 0
Duration: 245.32 seconds
Average: 0.49 seconds per VM

Namespace Statistics:
  Total namespaces used: 45
  Newly created: 45
  Reused existing: 0

Randomized Specifications:
  CPU cores:   min=1, max=4, avg=2.5
  Memory (Gi): min=1, max=8, avg=4.3
  Disk (Gi):   min=10, max=50, avg=29.7

VMs per Namespace:
  min=1, max=20, avg=11.1
================================================================================
```

## Important Notes

- **VMs are never started** (`spec.running: false`) - they exist only for performance measurement
- **Sparse disk allocation** is used to avoid pre-allocating storage
- **Lightweight images** (cirros) are used to minimize resource overhead
- **Multi-namespace distribution** simulates realistic multi-tenant environments
- **Automatic namespace management** - namespaces are created and deleted as needed
- **Namespace reuse** - existing labeled namespaces are reused instead of creating duplicates
- Maximum 999 namespaces supported (qe-ns-001 through qe-ns-999)
- VMs use `emptyDisk` volumes which don't consume actual storage until started
- Each namespace can contain 1-20 VMs (randomly determined)

## Performance Testing Use Cases

This script is designed for:
- Console performance testing under load
- Multi-tenant scenario testing
- Namespace-scoped operations performance
- API server stress testing
- Controller/operator scalability testing
- UI rendering performance with many resources
- Search and filtering performance analysis across namespaces
- RBAC and multi-namespace isolation testing

## Cleanup

Always delete test VMs after testing:
```bash
./scripts/cnv_scale_vms.py delete
```

Or verify what would be deleted first:
```bash
./scripts/cnv_scale_vms.py delete --dry-run
```

## Troubleshooting

**"Error loading kubeconfig"**
- Ensure `KUBECONFIG` environment variable is set or `~/.kube/config` exists
- Verify cluster access with `kubectl cluster-info`

**"Error creating VM: Forbidden"**
- Check RBAC permissions for VirtualMachine resources
- Ensure you have create/delete permissions in the target namespace

**"kubernetes module not found"**
- Install dependencies: `pip install -r requirements.txt`

## License

This script is provided as-is for testing and performance measurement purposes
