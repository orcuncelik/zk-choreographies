#!/usr/bin/env python3
"""
run_choreography.py

Generic one-click runner for ANY ZK Choreography end-to-end.
Modular Python port of run_choreography.sh (which remains untouched).

Usage:
    python3 run_choreography.py <bpmn-file> [OPTIONS]

Arguments:
    <bpmn-file>           Path to BPMN file (relative to project root)

Options:
    --e2e <script>        E2E Python script to run
    --l1-test <file>      L1 Ethereum Hardhat test file
    --zksync-test <file>  zkSync ERA Hardhat test file
    --name <name>         Override display name
    --clean               Delete cached proving keys (force recompile)
    --skip-zksync         Skip zkSync ERA gas tests
    --skip-gas            Skip all Hardhat gas tests
    --no-rebuild          Skip go build and npm install
"""

import argparse
import atexit
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════════

ROOT = Path(__file__).resolve().parent
BPMN_DIR = ROOT / "bpmn-service"
EXEC_DIR = ROOT / "execution-service"
SOL_DIR = ROOT / "solidity"
E2E_DIR = ROOT / "e2e"
PUBLIC_DIR = EXEC_DIR / "files" / "public"
NODE23_BIN = "/opt/homebrew/Cellar/node/23.10.0_1/bin"

LOG_DIR = Path("/tmp")
EXEC_LOG = LOG_DIR / "zk-exec-service.log"
BPMN_LOG = LOG_DIR / "zk-bpmn-service.log"
ANVIL_ZKSYNC_LOG = LOG_DIR / "zk-anvil-zksync.log"
E2E_LOG = LOG_DIR / "zk-e2e.log"
L1_LOG = LOG_DIR / "zk-l1-hardhat.log"
ZKSYNC_LOG = LOG_DIR / "zk-zksync-hardhat.log"
REPORT_PATH = LOG_DIR / "zk-choreography-report.txt"


# ═══════════════════════════════════════════════════════════════════════════════
# ANSI output helpers
# ═══════════════════════════════════════════════════════════════════════════════

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def log(msg: str):
    print(f"{CYAN}[{_ts()}]{RESET} {msg}")


def ok(msg: str):
    print(f"  {GREEN}+{RESET}  {msg}")


def warn(msg: str):
    print(f"  {YELLOW}!{RESET}  {msg}")


def info(msg: str):
    print(f"  {CYAN}-{RESET}  {msg}")


def die(msg: str):
    print(f"  {RED}x{RESET}  {msg}", file=sys.stderr)
    sys.exit(1)


def header(title: str):
    w = 54
    line = "-" * w
    print()
    print(f"{BOLD}{CYAN}+{line}+{RESET}")
    print(f"{BOLD}{CYAN}|  {title:<{w}}|{RESET}")
    print(f"{BOLD}{CYAN}+{line}+{RESET}")


# ═══════════════════════════════════════════════════════════════════════════════
# Name conversion
# ═══════════════════════════════════════════════════════════════════════════════

def to_snake(name: str) -> str:
    """'supply-chain' or 'Supply_Chain' -> 'supply_chain'"""
    return name.replace("-", "_").lower()


def to_pascal(name: str) -> str:
    """'supply-chain' -> 'SupplyChain'"""
    return "".join(part.capitalize() for part in re.split(r"[-_]", name) if part)


def to_display(name: str) -> str:
    """'supply-chain' -> 'Supply Chain'"""
    return " ".join(part.capitalize() for part in re.split(r"[-_]", name) if part)


# ═══════════════════════════════════════════════════════════════════════════════
# Auto-discovery
# ═══════════════════════════════════════════════════════════════════════════════

def discover_file(candidates: list[Path]) -> Optional[Path]:
    """Return the first candidate path that exists, or None."""
    for c in candidates:
        if c.is_file():
            return c
    return None


def discover_e2e(snake: str) -> Optional[Path]:
    return discover_file([
        E2E_DIR / f"{snake}_e2e.py",
        E2E_DIR / f"{snake}.py",
        E2E_DIR / "e2e.py",
    ])


def discover_l1_test(pascal: str) -> Optional[Path]:
    return discover_file([
        SOL_DIR / "test" / f"InstanceManager{pascal}.ts",
        SOL_DIR / "test" / "InstanceManager.ts",
    ])


def discover_zksync_test(pascal: str) -> Optional[Path]:
    return discover_file([
        SOL_DIR / "test" / f"InstanceManagerZkSync{pascal}.ts",
        SOL_DIR / "test" / "InstanceManagerZkSync.ts",
    ])


def detect_proofs_json(test_file: Path) -> Optional[str]:
    """Extract proofs JSON relative path from a test file."""
    try:
        text = test_file.read_text()
        m = re.search(r"test/[^'\"]*\.json", text)
        return m.group(0) if m else None
    except OSError:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# anvil-zksync binary discovery
# ═══════════════════════════════════════════════════════════════════════════════

def find_anvil_zksync() -> Optional[Path]:
    """Find the highest-version anvil-zksync binary under the hardhat cache."""
    zk_dir = Path.home() / ".cache" / "hardhat-nodejs" / "zksync-memory-node"
    if not zk_dir.is_dir():
        return None
    versions = []
    for entry in zk_dir.iterdir():
        if re.match(r"^\d+\.\d+\.\d+$", entry.name) and entry.is_file() and os.access(entry, os.X_OK):
            versions.append(entry)
    if not versions:
        return None
    versions.sort(key=lambda p: list(map(int, p.name.split("."))))
    return versions[-1]


# ═══════════════════════════════════════════════════════════════════════════════
# Process management
# ═══════════════════════════════════════════════════════════════════════════════

_background_processes: list[subprocess.Popen] = []


def _cleanup():
    """Kill all background processes started by this script."""
    for proc in _background_processes:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass


atexit.register(_cleanup)
signal.signal(signal.SIGINT, lambda *_: sys.exit(1))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))


def start_background(cmd: list[str], log_path: Path, cwd: Path, label: str) -> subprocess.Popen:
    """Start a process in the background, redirect output to log_path."""
    log_fh = open(log_path, "w")
    proc = subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT, cwd=cwd)
    _background_processes.append(proc)
    ok(f"{label}  PID={proc.pid}  ->  {log_path}")
    return proc


def wait_for_http(url: str, name: str, max_seconds: int = 120):
    """Poll a URL until it responds, or die after max_seconds."""
    log(f"Waiting for {name}...")
    for i in range(1, max_seconds + 1):
        try:
            subprocess.run(
                ["curl", "-s", "--connect-timeout", "1", "--max-time", "3", "-o", "/dev/null", url],
                check=True, capture_output=True,
            )
            ok(f"{name} ready after {i}s")
            return
        except subprocess.CalledProcessError:
            time.sleep(1)
    die(f"{name} did not respond within {max_seconds}s")


def free_port(port: int):
    """Kill any process listening on the given port."""
    result = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True)
    pids = result.stdout.strip()
    if pids:
        for pid in pids.split("\n"):
            subprocess.run(["kill", pid.strip()], capture_output=True)
        time.sleep(0.5)
        ok(f"Killed PID(s) on port {port}")
    else:
        ok(f"Port {port} is free")


# ═══════════════════════════════════════════════════════════════════════════════
# Build helpers
# ═══════════════════════════════════════════════════════════════════════════════

def build_execution_service():
    log("go build...")
    result = subprocess.run(["go", "build", "-o", "execution-service", "."], cwd=EXEC_DIR)
    if result.returncode != 0:
        die("go build failed")
    ok("execution-service built")


def install_bpmn_deps():
    log("npm install...")
    result = subprocess.run(["npm", "install", "--silent"], cwd=BPMN_DIR, capture_output=True)
    if result.returncode != 0:
        die("npm install failed")
    ok("Dependencies up to date")


# ═══════════════════════════════════════════════════════════════════════════════
# Log parsing
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ZkTimingMetrics:
    """Off-chain ZK timing metrics collected from service and e2e logs.

    Four distinct phases are measured:

    1. COMPILE (one-time, cached to disk)
       gnark frontend compiles the circuit definition (Go structs) into an R1CS
       constraint system. Only runs when no cached .constraint_system file exists.
       Measured from: execution-service log "TIMING compile <circuit>"

    2. SETUP (one-time, cached to disk)
       Groth16 trusted setup over the R1CS: generates the proving key (pk) and
       verification key (vk). Only runs when no cached .proving_key file exists.
       This is the most expensive one-time cost (~5-10s per circuit).
       Measured from: execution-service log "TIMING setup <circuit>"

    3. WITNESS (per proof, negligible ~0.2-0.3 ms)
       Constructs the full witness (public + private inputs) for a specific proof
       request. Runs every time a proof is generated but takes <0.1% of proof time.
       Measured from: e2e output or API response proof.timing.witnessMs

    4. PROOF (per proof, dominant cost ~950-1460 ms)
       Groth16 prover computes the ZK proof from the witness and proving key.
       This is the main per-transaction computational cost.
       Measured from: e2e output or API response proof.timing.proofMs
    """
    # Phase 1: Compile (one-time) — circuit definition -> R1CS constraint system
    compile_inst: str = ""
    compile_trans: str = ""
    compile_term: str = ""
    # Phase 2: Setup (one-time) — R1CS -> proving key + verification key (Groth16)
    setup_inst: str = ""
    setup_trans: str = ""
    setup_term: str = ""
    # Phase 3: Witness (per proof) — public/private inputs -> full witness assignment
    inst_witness: str = ""
    term_witness: str = ""
    avg_witness: str = ""
    # Phase 4: Proof (per proof) — witness + proving key -> Groth16 proof
    inst_proof: str = ""
    term_proof: str = ""
    avg_proof: str = ""
    # Total wall-clock time per proof (includes HTTP overhead)
    inst_total: str = ""
    term_total: str = ""
    avg_total: str = ""
    # Constraint counts (from execution-service log at startup)
    constraints_inst: str = ""
    constraints_trans: str = ""
    constraints_term: str = ""


def parse_zk_timing(exec_log: Path, e2e_log: Path) -> ZkTimingMetrics:
    """Extract ZK compile/setup/proof timing from service and e2e logs."""
    m = ZkTimingMetrics()

    def _svc_ms(pattern: str) -> str:
        try:
            for line in exec_log.read_text().splitlines():
                if pattern in line:
                    parts = line.split()
                    if parts:
                        return parts[-1].replace("ms", "")
        except OSError:
            pass
        return ""

    m.compile_inst = _svc_ms("TIMING compile instantiation")
    m.compile_trans = _svc_ms("TIMING compile transition")
    m.compile_term = _svc_ms("TIMING compile termination")
    m.setup_inst = _svc_ms("TIMING setup instantiation")
    m.setup_trans = _svc_ms("TIMING setup transition")
    m.setup_term = _svc_ms("TIMING setup termination")

    # Parse constraint counts from execution-service log.
    # Format: "Instantiation constraint system has 107707 constraints"
    try:
        for line in exec_log.read_text().splitlines():
            cm = re.search(r"(\w+) constraint system has (\d+) constraints", line)
            if cm:
                name = cm.group(1).lower()
                count = cm.group(2)
                if name == "instantiation":
                    m.constraints_inst = count
                elif name == "transition":
                    m.constraints_trans = count
                elif name == "termination":
                    m.constraints_term = count
    except OSError:
        pass

    try:
        e2e_text = e2e_log.read_text()
    except OSError:
        return m

    # Enhanced format from supply_chain_e2e.py per-proof table.
    # Columns: Step  Compile  Setup  Witness(ms)  Proof(ms)  Total(s)
    #          [0]   [1]      [2]    [-3]         [-2]       [-1]
    #
    # IMPORTANT: parts[-3]=witness, parts[-2]=proof, parts[-1]=total.
    # Previously this was wrong (parts[-2] was assigned to witness, parts[-1] to proof).
    for line in e2e_text.splitlines():
        stripped = line.strip()
        if re.match(r"instantiation\s+N/A", stripped):
            parts = stripped.split()
            if len(parts) >= 6:
                m.inst_witness = parts[-3]
                m.inst_proof = parts[-2]
                m.inst_total = parts[-1]
        elif re.match(r"termination\s+N/A", stripped):
            parts = stripped.split()
            if len(parts) >= 6:
                m.term_witness = parts[-3]
                m.term_proof = parts[-2]
                m.term_total = parts[-1]
        elif "avg (excl. events)" in stripped:
            parts = stripped.split()
            if len(parts) >= 6:
                m.avg_witness = parts[-3]
                m.avg_proof = parts[-2]
                m.avg_total = parts[-1]

    # Fallback: simple "Proof generated in X.XXs" lines (weber_e2e.py format)
    if not m.inst_proof:
        proof_times = re.findall(r"Proof generated in ([0-9]+\.[0-9]+)s", e2e_text)
        if proof_times:
            avg_s = sum(float(t) for t in proof_times) / len(proof_times)
            m.avg_proof = f"~{avg_s * 1000:.0f} (ms, avg of {len(proof_times)})"

    return m


@dataclass
class GasMetrics:
    """Gas/fee metrics for a single network (L1 or zkSync)."""
    chain_id: str = ""
    proof_size_bytes: str = ""
    gas_inst: str = ""
    gas_trans: str = ""
    gas_term: str = ""
    exec_total: str = ""
    deploy_total: str = ""
    # Per-operation details
    inst_gasprice: str = ""
    inst_fee: str = ""
    inst_calldata: str = ""
    inst_inputs: str = ""
    inst_writes: str = ""  # zkSync only
    trans_gasprice: str = ""
    trans_fee: str = ""
    trans_calldata: str = ""
    trans_inputs: str = ""
    trans_writes: str = ""
    term_gasprice: str = ""
    term_fee: str = ""
    term_calldata: str = ""
    term_inputs: str = ""
    term_writes: str = ""
    # Contract bytecode sizes
    bytes_iv: str = ""
    bytes_tv: str = ""
    bytes_rv: str = ""
    bytes_im: str = ""
    # Deploy gas
    deploy_iv: str = ""
    deploy_tv: str = ""
    deploy_rv: str = ""
    deploy_im: str = ""
    # Deploy fees
    deploy_iv_fee: str = ""
    deploy_tv_fee: str = ""
    deploy_rv_fee: str = ""
    deploy_im_fee: str = ""
    # Totals
    deploy_tx_count: str = ""
    transition_count: str = ""
    exec_fee_total: str = ""
    deploy_fee_total: str = ""
    grand_fee_total: str = ""
    ran: bool = False


@dataclass
class ArtifactFile:
    """Single generated/cached artifact written by the local toolchain."""
    label: str
    path: Path
    size_bytes: int


@dataclass
class ArtifactMetrics:
    """Inventory of generated/cached ZK artifacts on disk."""
    files: list[ArtifactFile] = field(default_factory=list)
    total_bytes: int = 0


def _parse_field(log_text: str, key: str) -> str:
    """Parse a 'key: value' line from log output, extracting digits."""
    for line in log_text.splitlines():
        if f"  {key}:" in line:
            parts = line.split(": ", 1)
            if len(parts) == 2:
                nums = re.findall(r"[0-9,]+", parts[1])
                if nums:
                    return nums[0].replace(",", "")
    return ""


def _parse_deploy_gas_from_table(log_text: str, contract_name: str, exclude: str = "NOMATCH") -> str:
    """Parse deployment gas from hardhat gas-reporter Deployments table."""
    ansi_re = re.compile(r"\x1b\[[0-9;]*m")
    in_deployments = False
    for raw in log_text.splitlines():
        line = ansi_re.sub("", raw)
        if re.search(r"^[|│]\s+Deployments\s+[·│]", line):
            in_deployments = True
            continue
        if not in_deployments:
            continue
        if line.startswith("·---"):
            break
        if contract_name in line and not re.search(exclude, line):
            m = re.search(r"([0-9]{5,})", line)
            if m:
                return m.group(1)
    return ""


def fmt_bytes_human(size_bytes: int) -> str:
    """Format a byte count for human-readable reporting."""
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size_bytes)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{size_bytes} B"


def collect_artifact_metrics(proofs_abs: Path) -> ArtifactMetrics:
    """Collect the generated/cached artifact files relevant to a benchmark run."""
    specs = [
        ("Instantiation CS", PUBLIC_DIR / "instantiation.constraint_system"),
        ("Transition CS", PUBLIC_DIR / "transition.constraint_system"),
        ("Termination CS", PUBLIC_DIR / "termination.constraint_system"),
        ("Instantiation PK", PUBLIC_DIR / "instantiation.proving_key"),
        ("Transition PK", PUBLIC_DIR / "transition.proving_key"),
        ("Termination PK", PUBLIC_DIR / "termination.proving_key"),
        ("Instantiation VK", SOL_DIR / "contracts" / "InstantiationVerifier.sol"),
        ("Transition VK", SOL_DIR / "contracts" / "TransitionVerifier.sol"),
        ("Termination VK", SOL_DIR / "contracts" / "TerminationVerifier.sol"),
        ("Proofs JSON", proofs_abs),
    ]

    files: list[ArtifactFile] = []
    total_bytes = 0
    for label, path in specs:
        if not path.is_file():
            continue
        try:
            size_bytes = path.stat().st_size
        except OSError:
            continue
        files.append(ArtifactFile(label=label, path=path, size_bytes=size_bytes))
        total_bytes += size_bytes

    return ArtifactMetrics(files=files, total_bytes=total_bytes)


def parse_l1_gas(log_path: Path) -> GasMetrics:
    """Parse L1 Ethereum gas metrics from Hardhat test log."""
    g = GasMetrics()
    try:
        text = log_path.read_text()
    except OSError:
        return g

    g.ran = True

    # Per-call gas from console.log lines (exclude table rows with │)
    for line in text.splitlines():
        clean = line
        if "│" in clean:
            continue
        if "instantiate (real):" in clean:
            nums = re.findall(r"[0-9,]+", clean)
            if nums:
                g.gas_inst = nums[0].replace(",", "")
        elif "transition:" in clean and "count" not in clean.lower():
            nums = re.findall(r"[0-9,]+", clean)
            if nums:
                g.gas_trans = nums[0].replace(",", "")
        elif "terminate:" in clean:
            nums = re.findall(r"[0-9,]+", clean)
            if nums:
                g.gas_term = nums[0].replace(",", "")

    g.exec_total = _parse_field(text, "l1-exec-total")
    g.chain_id = _parse_field(text, "l1-chain-id")
    g.proof_size_bytes = _parse_field(text, "l1-proof-size-bytes")
    g.inst_gasprice = _parse_field(text, "l1-instantiate-gasprice")
    g.inst_fee = _parse_field(text, "l1-instantiate-fee")
    g.inst_calldata = _parse_field(text, "l1-instantiate-calldata-bytes")
    g.inst_inputs = _parse_field(text, "l1-instantiate-input-count")
    g.trans_gasprice = _parse_field(text, "l1-transition-gasprice")
    g.trans_fee = _parse_field(text, "l1-transition-fee")
    g.trans_calldata = _parse_field(text, "l1-transition-calldata-bytes")
    g.trans_inputs = _parse_field(text, "l1-transition-input-count")
    g.term_gasprice = _parse_field(text, "l1-terminate-gasprice")
    g.term_fee = _parse_field(text, "l1-terminate-fee")
    g.term_calldata = _parse_field(text, "l1-terminate-calldata-bytes")
    g.term_inputs = _parse_field(text, "l1-terminate-input-count")
    g.bytes_iv = _parse_field(text, "l1-deploy-iv-bytes")
    g.bytes_tv = _parse_field(text, "l1-deploy-tv-bytes")
    g.bytes_rv = _parse_field(text, "l1-deploy-rv-bytes")
    g.bytes_im = _parse_field(text, "l1-deploy-im-bytes")
    g.deploy_tx_count = _parse_field(text, "l1-deploy-tx-count")
    g.transition_count = _parse_field(text, "l1-transition-count")
    g.exec_fee_total = _parse_field(text, "l1-exec-total-fee")
    g.deploy_fee_total = _parse_field(text, "l1-deploy-total-fee")
    g.grand_fee_total = _parse_field(text, "l1-grand-total-fee")
    g.deploy_iv = _parse_field(text, "l1-deploy-iv-gas")
    g.deploy_tv = _parse_field(text, "l1-deploy-tv-gas")
    g.deploy_rv = _parse_field(text, "l1-deploy-rv-gas")
    g.deploy_im = _parse_field(text, "l1-deploy-im-gas")
    g.deploy_iv_fee = _parse_field(text, "l1-deploy-iv-fee")
    g.deploy_tv_fee = _parse_field(text, "l1-deploy-tv-fee")
    g.deploy_rv_fee = _parse_field(text, "l1-deploy-rv-fee")
    g.deploy_im_fee = _parse_field(text, "l1-deploy-im-fee")

    # Fallback: parse from gas-reporter Deployments table
    if not g.deploy_im:
        g.deploy_im = _parse_deploy_gas_from_table(text, "InstanceManager", "Verifier|Mock")
    if not g.deploy_iv:
        g.deploy_iv = _parse_deploy_gas_from_table(text, "InstantiationVerifier", "Mock")
    if not g.deploy_tv:
        g.deploy_tv = _parse_deploy_gas_from_table(text, "TransitionVerifier")
    if not g.deploy_rv:
        g.deploy_rv = _parse_deploy_gas_from_table(text, "TerminationVerifier")

    if all([g.deploy_im, g.deploy_iv, g.deploy_tv, g.deploy_rv]):
        g.deploy_total = str(int(g.deploy_im) + int(g.deploy_iv) + int(g.deploy_tv) + int(g.deploy_rv))

    return g


def parse_zksync_gas(log_path: Path) -> GasMetrics:
    """Parse zkSync ERA gas metrics from Hardhat test log."""
    g = GasMetrics()
    try:
        text = log_path.read_text()
    except OSError:
        return g

    g.ran = True

    # Per-call gas from receipt-based summary table
    for line in text.splitlines():
        if "│  instantiate (real)" in line:
            nums = re.findall(r"[0-9]{5,}", line)
            if nums:
                g.gas_inst = nums[0]
        elif "│  transition" in line:
            nums = re.findall(r"[0-9]{5,}", line)
            if nums:
                g.gas_trans = nums[0]
        elif "│  terminate" in line:
            nums = re.findall(r"[0-9]{5,}", line)
            if nums:
                g.gas_term = nums[0]

    g.exec_total = _parse_field(text, "zksync-exec-total")
    g.deploy_total = _parse_field(text, "zksync-deploy-total")
    g.chain_id = _parse_field(text, "zksync-chain-id")
    g.proof_size_bytes = _parse_field(text, "zksync-proof-size-bytes")
    g.inst_gasprice = _parse_field(text, "zksync-instantiate-gasprice")
    g.inst_fee = _parse_field(text, "zksync-instantiate-fee")
    g.inst_calldata = _parse_field(text, "zksync-instantiate-calldata-bytes")
    g.inst_inputs = _parse_field(text, "zksync-instantiate-input-count")
    g.inst_writes = _parse_field(text, "zksync-instantiate-storage-writes")
    g.trans_gasprice = _parse_field(text, "zksync-transition-gasprice")
    g.trans_fee = _parse_field(text, "zksync-transition-fee")
    g.trans_calldata = _parse_field(text, "zksync-transition-calldata-bytes")
    g.trans_inputs = _parse_field(text, "zksync-transition-input-count")
    g.trans_writes = _parse_field(text, "zksync-transition-storage-writes")
    g.term_gasprice = _parse_field(text, "zksync-terminate-gasprice")
    g.term_fee = _parse_field(text, "zksync-terminate-fee")
    g.term_calldata = _parse_field(text, "zksync-terminate-calldata-bytes")
    g.term_inputs = _parse_field(text, "zksync-terminate-input-count")
    g.term_writes = _parse_field(text, "zksync-terminate-storage-writes")
    g.bytes_iv = _parse_field(text, "zksync-deploy-iv-bytes")
    g.bytes_tv = _parse_field(text, "zksync-deploy-tv-bytes")
    g.bytes_rv = _parse_field(text, "zksync-deploy-rv-bytes")
    g.bytes_im = _parse_field(text, "zksync-deploy-im-bytes")
    g.deploy_tx_count = _parse_field(text, "zksync-deploy-tx-count")
    g.transition_count = _parse_field(text, "zksync-transition-count")
    g.deploy_fee_total = _parse_field(text, "zksync-deploy-total-fee")
    g.exec_fee_total = _parse_field(text, "zksync-exec-total-fee")
    g.grand_fee_total = _parse_field(text, "zksync-grand-total-fee")
    g.deploy_iv_fee = _parse_field(text, "zksync-deploy-iv-fee")
    g.deploy_tv_fee = _parse_field(text, "zksync-deploy-tv-fee")
    g.deploy_rv_fee = _parse_field(text, "zksync-deploy-rv-fee")
    g.deploy_im_fee = _parse_field(text, "zksync-deploy-im-fee")
    g.deploy_iv = _parse_field(text, "zksync-deploy-iv-gas")
    g.deploy_tv = _parse_field(text, "zksync-deploy-tv-gas")
    g.deploy_rv = _parse_field(text, "zksync-deploy-rv-gas")
    g.deploy_im = _parse_field(text, "zksync-deploy-im-gas")

    # Fallback: parse from deployments table
    if not all([g.deploy_iv, g.deploy_tv, g.deploy_rv, g.deploy_im]):
        if not g.deploy_iv:
            g.deploy_iv = _parse_deploy_gas_from_table(text, "InstantiationVerifier")
        if not g.deploy_tv:
            g.deploy_tv = _parse_deploy_gas_from_table(text, "TransitionVerifier")
        if not g.deploy_rv:
            g.deploy_rv = _parse_deploy_gas_from_table(text, "TerminationVerifier")
        if not g.deploy_im:
            g.deploy_im = _parse_deploy_gas_from_table(text, "InstanceManager")

    return g


# ═══════════════════════════════════════════════════════════════════════════════
# Network data (ETH price, gas prices)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ZkSyncFeeEstimate:
    """Fee estimate from zks_estimateFee for a single operation."""
    gas_limit: str = ""            # gas_limit from zks_estimateFee (base cost, no contract exec)
    max_fee_per_gas_wei: str = ""  # max_fee_per_gas in wei
    gas_per_pubdata: str = ""      # gas_per_pubdata_limit


@dataclass
class NetworkData:
    eth_usd: str = ""
    l1_gas_price_gwei: str = ""
    # zkSync: authoritative gas price from zks_estimateFee (preferred over eth_gasPrice)
    zksync_gas_price_gwei: str = ""
    zksync_max_fee_per_gas_wei: str = ""  # raw wei from zks_estimateFee
    zksync_gas_per_pubdata: str = ""      # gas_per_pubdata_limit
    # Per-operation estimates from zks_estimateFee
    zksync_est_inst: Optional[ZkSyncFeeEstimate] = None
    zksync_est_trans: Optional[ZkSyncFeeEstimate] = None
    zksync_est_term: Optional[ZkSyncFeeEstimate] = None


def _hex_to_gwei(hex_str: str) -> str:
    hex_str = hex_str.removeprefix("0x")
    if not hex_str:
        return ""
    dec = int(hex_str, 16)
    return f"{dec / 1_000_000_000:.4f}"


def _hex_to_dec(hex_str: str) -> str:
    hex_str = hex_str.removeprefix("0x")
    if not hex_str:
        return ""
    return str(int(hex_str, 16))


# ── ABI encoding helpers ─────────────────────────────────────────────────────

# Function selectors (keccak256 of canonical signature, first 4 bytes)
_SEL_INSTANTIATE = "5ebd9ab7"   # instantiate(uint256[8],uint256)
_SEL_TRANSITION  = "74ff1fcc"   # transition(uint256[8],uint256,uint256)
_SEL_TERMINATE   = "5432beb7"   # terminate(uint256[8],uint256)

# Well-known funded address for fee estimation (vitalik.eth)
_ESTIMATOR_FROM = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
# Dummy target (no contract here; zks_estimateFee still returns gas pricing)
_ESTIMATOR_TO   = "0x0000000000000000000000000000000000000002"

ZKSYNC_MAINNET_RPC = "https://mainnet.era.zksync.io"


def _abi_encode_uint256(n: int) -> str:
    """Encode a uint256 as 64 hex chars (32 bytes, zero-padded)."""
    return f"{n:064x}"


def _build_calldata(selector: str, proof: list[str], inputs: list[str]) -> str:
    """Build hex-encoded calldata (no 0x prefix) for a contract call."""
    parts = [selector]
    for elem in proof:
        parts.append(_abi_encode_uint256(int(elem)))
    for inp in inputs:
        parts.append(_abi_encode_uint256(int(inp)))
    return "".join(parts)


def _call_zks_estimate_fee(calldata_hex: str) -> Optional[ZkSyncFeeEstimate]:
    """Call zks_estimateFee on zkSync mainnet and parse the result."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "zks_estimateFee",
        "params": [{
            "from": _ESTIMATOR_FROM,
            "to": _ESTIMATOR_TO,
            "data": "0x" + calldata_hex,
        }],
        "id": 1,
    })
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "8", "-X", "POST",
             "-H", "Content-Type: application/json",
             "-d", payload, ZKSYNC_MAINNET_RPC],
            capture_output=True, text=True, timeout=10,
        )
        resp = json.loads(r.stdout)
        result = resp.get("result")
        if not result:
            return None
        est = ZkSyncFeeEstimate()
        est.gas_limit = _hex_to_dec(result.get("gas_limit", ""))
        est.max_fee_per_gas_wei = _hex_to_dec(result.get("max_fee_per_gas", ""))
        est.gas_per_pubdata = _hex_to_dec(result.get("gas_per_pubdata_limit", ""))
        return est
    except Exception:
        return None


def estimate_zksync_fees(proofs_path: Path) -> tuple[
    Optional[ZkSyncFeeEstimate],
    Optional[ZkSyncFeeEstimate],
    Optional[ZkSyncFeeEstimate],
]:
    """Call zks_estimateFee on zkSync mainnet with real calldata from proofs JSON.

    Returns (inst_est, trans_est, term_est).  Each may be None on failure.
    Note: gas_limit from zks_estimateFee is the BASE cost (calldata + intrinsic),
    not the full execution cost (no contract at the dummy address).
    The authoritative value is max_fee_per_gas (mainnet gas price).
    """
    try:
        proofs = json.loads(proofs_path.read_text())
    except Exception:
        return None, None, None

    if len(proofs) < 3:
        return None, None, None

    # Proof 0 = instantiation (1 input), Proof 1 = transition (2 inputs), last = termination (1 input)
    p_inst = proofs[0]
    p_trans = proofs[1]
    p_term = proofs[-1]

    log("Calling zks_estimateFee on zkSync mainnet with real proof calldata...")

    cd_inst  = _build_calldata(_SEL_INSTANTIATE, p_inst["value"],  p_inst["input"][:1])
    cd_trans = _build_calldata(_SEL_TRANSITION,  p_trans["value"], p_trans["input"][:2])
    cd_term  = _build_calldata(_SEL_TERMINATE,   p_term["value"],  p_term["input"][:1])

    est_inst  = _call_zks_estimate_fee(cd_inst)
    est_trans = _call_zks_estimate_fee(cd_trans)
    est_term  = _call_zks_estimate_fee(cd_term)

    if est_inst:
        ok(f"zks_estimateFee: max_fee_per_gas={est_inst.max_fee_per_gas_wei} wei  "
           f"gas_per_pubdata={est_inst.gas_per_pubdata}")
    else:
        warn("zks_estimateFee failed for instantiate")

    return est_inst, est_trans, est_term


def fetch_network_data(proofs_path: Optional[Path] = None) -> NetworkData:
    """Fetch live ETH price, L1 gas price, and zkSync fee estimates."""
    log("Fetching live ETH price and mainnet gas prices...")
    nd = NetworkData()

    # ETH/USD
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "8",
             "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"],
            capture_output=True, text=True, timeout=10,
        )
        m = re.search(r'"usd":([0-9.]+)', r.stdout)
        if m:
            nd.eth_usd = m.group(1)
            ok(f"ETH price : ${nd.eth_usd}")
        else:
            warn("ETH price fetch failed (USD estimates will be omitted)")
    except Exception:
        warn("ETH price fetch failed")

    # L1 gas price (Ethereum mainnet)
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "8", "-X", "POST",
             "-H", "Content-Type: application/json",
             "-d", '{"jsonrpc":"2.0","method":"eth_gasPrice","id":1}',
             "https://eth.llamarpc.com"],
            capture_output=True, text=True, timeout=10,
        )
        m = re.search(r'"result":"(0x[0-9a-fA-F]+)"', r.stdout)
        if m:
            nd.l1_gas_price_gwei = _hex_to_gwei(m.group(1))
            ok(f"L1 gas price: {nd.l1_gas_price_gwei} gwei")
        else:
            warn("L1 gas price fetch failed")
    except Exception:
        warn("L1 gas price fetch failed")

    # zkSync: use zks_estimateFee with real proof calldata for authoritative pricing
    if proofs_path and proofs_path.is_file():
        est_inst, est_trans, est_term = estimate_zksync_fees(proofs_path)
        nd.zksync_est_inst = est_inst
        nd.zksync_est_trans = est_trans
        nd.zksync_est_term = est_term
        # Use max_fee_per_gas from the estimate as the authoritative zkSync gas price
        if est_inst and est_inst.max_fee_per_gas_wei:
            wei = int(est_inst.max_fee_per_gas_wei)
            nd.zksync_max_fee_per_gas_wei = est_inst.max_fee_per_gas_wei
            nd.zksync_gas_price_gwei = f"{wei / 1_000_000_000:.4f}"
            nd.zksync_gas_per_pubdata = est_inst.gas_per_pubdata
            ok(f"zkSync gas price (from zks_estimateFee): {nd.zksync_gas_price_gwei} gwei")
    else:
        # Fallback: use eth_gasPrice from zkSync mainnet
        try:
            r = subprocess.run(
                ["curl", "-s", "--max-time", "8", "-X", "POST",
                 "-H", "Content-Type: application/json",
                 "-d", '{"jsonrpc":"2.0","method":"eth_gasPrice","id":1}',
                 ZKSYNC_MAINNET_RPC],
                capture_output=True, text=True, timeout=10,
            )
            m = re.search(r'"result":"(0x[0-9a-fA-F]+)"', r.stdout)
            if m:
                nd.zksync_gas_price_gwei = _hex_to_gwei(m.group(1))
                nd.zksync_max_fee_per_gas_wei = _hex_to_dec(m.group(1))
                ok(f"zkSync gas price (eth_gasPrice fallback): {nd.zksync_gas_price_gwei} gwei")
            else:
                warn("zkSync gas price fetch failed")
        except Exception:
            warn("zkSync gas price fetch failed")

    return nd


# ═══════════════════════════════════════════════════════════════════════════════
# Report formatting helpers
# ═══════════════════════════════════════════════════════════════════════════════

def fmt_gas(n: str) -> str:
    """Format a gas number with comma separators, or return as-is."""
    if n and n.isdigit():
        return f"{int(n):,}"
    return n or "?"


def calc_usd_wei(wei: str, eth_usd: str) -> str:
    if not wei or not eth_usd:
        return "N/A"
    try:
        result = int(wei) * float(eth_usd) / 1e18
        return f"${result:.2f}"
    except (ValueError, ZeroDivisionError):
        return "N/A"


def calc_mainnet_fee_wei(gas_used: str, gas_price_gwei: str = "", gas_price_wei: str = "") -> str:
    """Compute fee in wei from gas units and mainnet gas price.

    Prefers gas_price_wei (exact) over gas_price_gwei (float conversion).
    """
    if not gas_used:
        return ""
    try:
        if gas_price_wei:
            fee = int(gas_used) * int(gas_price_wei)
        elif gas_price_gwei:
            fee = int(gas_used) * float(gas_price_gwei) * 1e9
        else:
            return ""
        return str(int(fee))
    except (ValueError, ZeroDivisionError):
        return ""


def calc_usd_gas(gas_used: str, eth_usd: str, gas_price_gwei: str = "", gas_price_wei: str = "") -> str:
    """Compute USD cost from gas units, mainnet gas price, and ETH price."""
    fee_wei = calc_mainnet_fee_wei(gas_used, gas_price_gwei=gas_price_gwei, gas_price_wei=gas_price_wei)
    return calc_usd_wei(fee_wei, eth_usd)


def safe_ratio(a: str, b: str) -> str:
    try:
        return f"{int(a) / int(b):.2f}"
    except (ValueError, ZeroDivisionError):
        return "N/A"


# ═══════════════════════════════════════════════════════════════════════════════
# Metadata collection
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Metadata:
    node_ver: str = "?"
    go_ver: str = "?"
    gnark_ver: str = "?"
    gnark_crypto_ver: str = "?"
    hardhat_ver: str = "?"
    sol_compiler_ver: str = "?"
    zksolc_ver: str = "?"
    # Hardware
    cpu_model: str = "?"
    ram_gb: str = "?"
    os_version: str = "?"
    # Circuit parameter set
    circuit_factor: str = "?"
    max_places: str = "?"
    max_participants: str = "?"
    max_transitions: str = "?"
    max_messages: str = "?"


def collect_metadata() -> Metadata:
    md = Metadata()
    try:
        md.node_ver = subprocess.run(["node", "--version"], capture_output=True, text=True).stdout.strip()
    except Exception:
        pass
    try:
        out = subprocess.run(["go", "version"], capture_output=True, text=True).stdout.strip()
        parts = out.split()
        md.go_ver = parts[2] if len(parts) >= 3 else out
    except Exception:
        pass

    go_mod = EXEC_DIR / "go.mod"
    if go_mod.is_file():
        text = go_mod.read_text()
        m = re.search(r"github\.com/consensys/gnark\s+(\S+)", text)
        if m:
            md.gnark_ver = m.group(1)
        m = re.search(r"github\.com/consensys/gnark-crypto\s+(\S+)", text)
        if m:
            md.gnark_crypto_ver = m.group(1)

    try:
        out = subprocess.run(
            ["node", "-e", "console.log(require('./node_modules/hardhat/package.json').version)"],
            capture_output=True, text=True, cwd=SOL_DIR,
        ).stdout.strip()
        if out:
            md.hardhat_ver = out
    except Exception:
        pass

    hh_config = SOL_DIR / "hardhat.config.ts"
    if hh_config.is_file():
        text = hh_config.read_text()
        versions = re.findall(r"[0-9]+\.[0-9]+\.[0-9]+", text)
        if versions:
            md.sol_compiler_ver = versions[0]
        if len(versions) > 1:
            md.zksolc_ver = versions[1]

    # Hardware detection (macOS)
    try:
        out = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                             capture_output=True, text=True).stdout.strip()
        if out:
            md.cpu_model = out
    except Exception:
        pass
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                             capture_output=True, text=True).stdout.strip()
        if out:
            md.ram_gb = f"{int(out) // (1024**3)} GB"
    except Exception:
        pass
    try:
        import platform
        md.os_version = f"{platform.system()} {platform.release()} ({platform.machine()})"
    except Exception:
        pass

    # Circuit parameter set from domain/model.go
    model_go = EXEC_DIR / "domain" / "model.go"
    if model_go.is_file():
        text = model_go.read_text()
        m = re.search(r"const\s+factor\s*=\s*(\d+)", text)
        if m:
            md.circuit_factor = m.group(1)
        for const_name, attr in [
            ("MaxPlaceCount", "max_places"),
            ("MaxParticipantCount", "max_participants"),
            ("MaxTransitionCount", "max_transitions"),
            ("MaxMessageCount", "max_messages"),
        ]:
            cm = re.search(rf"const\s+{const_name}\s*=\s*.*?//\s*(\d+)", text)
            if cm:
                setattr(md, attr, cm.group(1))
            else:
                # Compute from base * factor if no inline comment
                bm = re.search(rf"const\s+Base{const_name.removeprefix('Max')}\s*=\s*(\d+)", text)
                if bm and md.circuit_factor.isdigit():
                    setattr(md, attr, str(int(bm.group(1)) * int(md.circuit_factor)))

    return md


# ═══════════════════════════════════════════════════════════════════════════════
# Report generation
# ═══════════════════════════════════════════════════════════════════════════════

def generate_report(
    display_name: str,
    bpmn_arg: str,
    meta: Metadata,
    timing: ZkTimingMetrics,
    artifacts: ArtifactMetrics,
    l1: GasMetrics,
    zk: GasMetrics,
    nd: NetworkData,
    e2e_ran: bool,
    zksync_node_started: bool,
    e2e_script: Optional[Path],
    wall_time: float,
) -> str:
    """Generate the final report as a string."""
    SEP = "=" * 72
    lines: list[str] = []
    p = lines.append  # shorthand

    p(SEP)
    p(f"  ZK CHOREOGRAPHY REPORT: {display_name}")
    p(f"  Generated : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    p(f"  BPMN file : {bpmn_arg}")
    p(SEP)
    p("")

    # A. Experiment Metadata
    p("  +-- A. Experiment Metadata ---------------------------------------+")
    p("  |")
    p(f"  |  {'Date':<26}  {time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    p(f"  |  {'Choreography':<26}  {display_name}")
    p(f"  |  {'BPMN file':<26}  {bpmn_arg}")
    p("  |")
    p(f"  |  Hardware:")
    p(f"  |  {'CPU':<26}  {meta.cpu_model}")
    p(f"  |  {'RAM':<26}  {meta.ram_gb}")
    p(f"  |  {'OS':<26}  {meta.os_version}")
    p("  |")
    p(f"  |  Software:")
    p(f"  |  {'Node.js':<26}  {meta.node_ver}")
    p(f"  |  {'Go':<26}  {meta.go_ver}")
    p(f"  |  {'gnark':<26}  {meta.gnark_ver}")
    p(f"  |  {'gnark-crypto':<26}  {meta.gnark_crypto_ver}")
    p(f"  |  {'Hardhat':<26}  {meta.hardhat_ver}")
    p(f"  |  {'Solc':<26}  {meta.sol_compiler_ver}")
    p(f"  |  {'zksolc':<26}  {meta.zksolc_ver}")
    p("  |")
    p(f"  |  ZK Configuration:")
    p(f"  |  {'Proof system':<26}  Groth16")
    p(f"  |  {'Curve':<26}  BN254")
    proof_size = l1.proof_size_bytes or zk.proof_size_bytes or "?"
    proof_size_desc = "serialized proof.value as uint256[8] (8 x 32-byte field elements; excludes public inputs)"
    p(f"  |  {'Proof size':<26}  {proof_size} bytes ({proof_size_desc})")
    p(f"  |  {'Optimizer':<26}  disabled (both L1 and zkSync)")
    p(f"  |  {'Parameter set (factor)':<26}  {meta.circuit_factor}x  (base sizes x {meta.circuit_factor})")
    p(f"  |  {'Max places':<26}  {meta.max_places}")
    p(f"  |  {'Max participants':<26}  {meta.max_participants}")
    p(f"  |  {'Max transitions':<26}  {meta.max_transitions}")
    p(f"  |  {'Max messages':<26}  {meta.max_messages}")
    if timing.constraints_inst:
        p("  |")
        p(f"  |  Constraint Counts (R1CS):")
        p(f"  |  {'instantiation':<26}  {fmt_gas(timing.constraints_inst)}")
        p(f"  |  {'transition':<26}  {fmt_gas(timing.constraints_trans)}")
        p(f"  |  {'termination':<26}  {fmt_gas(timing.constraints_term)}")
    if l1.chain_id:
        p(f"  |  {'L1 chain ID':<26}  {l1.chain_id}")
    if zk.chain_id:
        p(f"  |  {'zkSync chain ID':<26}  {zk.chain_id}")
    p("  |")
    p("  +------------------------------------------------------------------+")
    p("")

    # B. On-Chain Deployment
    if l1.ran or zk.ran:
        p("  +-- B. On-Chain Deployment ----------------------------------------+")
        p("  |")
        if l1.ran and l1.bytes_iv:
            p(f"  |  L1 Ethereum  (chain {l1.chain_id or '?'} | tx count: {l1.deploy_tx_count or '4'})")
            p(f"  |  {'Contract':<28}  {'Bytecode':>10}  {'Deploy gas':>12}")
            p(f"  |  {'-'*28}  {'-'*10}  {'-'*12}")
            p(f"  |  {'InstantiationVerifier':<28}  {(l1.bytes_iv or '?') + ' B':>10}  {fmt_gas(l1.deploy_iv):>12}")
            p(f"  |  {'TransitionVerifier':<28}  {(l1.bytes_tv or '?') + ' B':>10}  {fmt_gas(l1.deploy_tv):>12}")
            p(f"  |  {'TerminationVerifier':<28}  {(l1.bytes_rv or '?') + ' B':>10}  {fmt_gas(l1.deploy_rv):>12}")
            p(f"  |  {'InstanceManager':<28}  {(l1.bytes_im or '?') + ' B':>10}  {fmt_gas(l1.deploy_im):>12}")
            p(f"  |  {'-'*28}  {'-'*10}  {'-'*12}")
            p(f"  |  {'TOTAL':<28}  {'':>10}  {fmt_gas(l1.deploy_total):>12}")
            p("  |")
        if zk.ran and zk.bytes_iv:
            p(f"  |  zkSync ERA  (chain {zk.chain_id or '?'} | tx count: {zk.deploy_tx_count or '4'})")
            p(f"  |  {'Contract':<28}  {'Bytecode':>10}  {'Deploy gas':>12}  {'Deploy fee (wei)':>22}")
            p(f"  |  {'-'*28}  {'-'*10}  {'-'*12}  {'-'*22}")
            for name, byt, dg, df in [
                ("InstantiationVerifier", zk.bytes_iv, zk.deploy_iv, zk.deploy_iv_fee),
                ("TransitionVerifier", zk.bytes_tv, zk.deploy_tv, zk.deploy_tv_fee),
                ("TerminationVerifier", zk.bytes_rv, zk.deploy_rv, zk.deploy_rv_fee),
                ("InstanceManager", zk.bytes_im, zk.deploy_im, zk.deploy_im_fee),
            ]:
                p(f"  |  {name:<28}  {(byt or '?') + ' B':>10}  {fmt_gas(dg):>12}  {df or '?':>22}")
            p(f"  |  {'-'*28}  {'-'*10}  {'-'*12}  {'-'*22}")
            p(f"  |  {'TOTAL':<28}  {'':>10}  {fmt_gas(zk.deploy_total):>12}  {zk.deploy_fee_total or '?':>22}")
            p("  |")
        p("  +------------------------------------------------------------------+")
        p("")

    # C. On-Chain Execution
    if (l1.ran and l1.gas_inst) or (zk.ran and zk.gas_inst):
        p("  +-- C. On-Chain Execution ----------------------------------------+")
        p("  |")
        if nd.l1_gas_price_gwei or nd.zksync_gas_price_gwei:
            zk_src = "zks_estimateFee" if nd.zksync_est_inst else "eth_gasPrice"
            p(f"  |  Mainnet gas prices: L1={nd.l1_gas_price_gwei or 'N/A'} gwei, zkSync={nd.zksync_gas_price_gwei or 'N/A'} gwei (via {zk_src})")
            if nd.zksync_gas_per_pubdata:
                p(f"  |  zkSync gas_per_pubdata_limit: {nd.zksync_gas_per_pubdata}")
            p("  |")
        for op, lg, zg in [
            ("instantiate",
             (l1.gas_inst, l1.inst_calldata, l1.inst_inputs),
             (zk.gas_inst, zk.inst_calldata, zk.inst_inputs, zk.inst_writes)),
            ("transition",
             (l1.gas_trans, l1.trans_calldata, l1.trans_inputs),
             (zk.gas_trans, zk.trans_calldata, zk.trans_inputs, zk.trans_writes)),
            ("terminate",
             (l1.gas_term, l1.term_calldata, l1.term_inputs),
             (zk.gas_term, zk.term_calldata, zk.term_inputs, zk.term_writes)),
        ]:
            p(f"  |  Operation : {op}")
            if l1.ran and lg[0]:
                l1_fee = calc_mainnet_fee_wei(lg[0], gas_price_gwei=nd.l1_gas_price_gwei)
                p(f"  |    L1  gasUsed={fmt_gas(lg[0])}  fee={l1_fee or '?'} wei  calldata={lg[1] or '?'} B  inputs={lg[2] or '?'}")
            if zk.ran and zg[0]:
                zk_fee = calc_mainnet_fee_wei(zg[0], gas_price_wei=nd.zksync_max_fee_per_gas_wei)
                wr = f"  storageWrites={zg[3]}" if len(zg) > 3 and zg[3] else ""
                p(f"  |    ZK  gasUsed={fmt_gas(zg[0])}  fee={zk_fee or '?'} wei  calldata={zg[1] or '?'} B  inputs={zg[2] or '?'}{wr}")
            p("  |")
        p("  +------------------------------------------------------------------+")
        p("")

    # D. Off-Chain ZK Workload
    p("  +-- D. Off-Chain ZK Workload -------------------------------------+")
    p("  |")
    p("  |  Four phases of ZK proof generation:")
    p("  |")
    p("  |  [1] Compile  -- circuit definition (Go) -> R1CS constraint system")
    p("  |  [2] Setup    -- R1CS -> proving key + verification key (Groth16)")
    p("  |      Both are ONE-TIME costs, cached to disk after first run.")
    p("  |      Amortised to zero for subsequent runs.")
    p("  |")
    p("  |  [3] Witness  -- public/private inputs -> full witness assignment")
    p("  |  [4] Proof    -- witness + proving key -> Groth16 proof")
    p("  |      Both run PER PROOF. Witness is negligible (<0.3 ms).")
    p("  |      Proof generation is the dominant per-transaction cost.")
    p("  |")
    p("  |  [1]+[2] Circuit Initialization (one-time, cached after first run)")
    p(f"  |  {'Circuit':<18}  {'Compile (ms)':>15}  {'Setup (ms)':>15}")
    p(f"  |  {'-'*18}  {'-'*15}  {'-'*15}")
    if timing.compile_inst:
        p(f"  |  {'instantiation':<18}  {timing.compile_inst:>15}  {timing.setup_inst:>15}")
        p(f"  |  {'transition':<18}  {timing.compile_trans:>15}  {timing.setup_trans:>15}")
        p(f"  |  {'termination':<18}  {timing.compile_term:>15}  {timing.setup_term:>15}")
    else:
        p(f"  |  {'instantiation':<18}  {'(cached)':>15}  {'(cached)':>15}")
        p(f"  |  {'transition':<18}  {'(cached)':>15}  {'(cached)':>15}")
        p(f"  |  {'termination':<18}  {'(cached)':>15}  {'(cached)':>15}")
        p("  |  -> Use --clean to force recompile and capture these values")
    p("  |")
    p("  |  [3]+[4] Per-Proof Timing (steady-state, keys loaded from disk)")
    p(f"  |  {'Proof type':<30}  {'Witness (ms)':>14}  {'Proof (ms)':>14}  {'Total (s)':>12}")
    p(f"  |  {'-'*30}  {'-'*14}  {'-'*14}  {'-'*12}")
    if timing.inst_proof:
        p(f"  |  {'instantiation':<30}  {timing.inst_witness or 'N/A':>14}  {timing.inst_proof or 'N/A':>14}  {timing.inst_total or 'N/A':>12}")
        p(f"  |  {'transition (avg)':<30}  {timing.avg_witness or 'N/A':>14}  {timing.avg_proof or 'N/A':>14}  {timing.avg_total or 'N/A':>12}")
        p(f"  |  {'termination':<30}  {timing.term_witness or 'N/A':>14}  {timing.term_proof or 'N/A':>14}  {timing.term_total or 'N/A':>12}")
    elif timing.avg_proof:
        p(f"  |  {'avg (all proofs)':<30}  {'N/A':>14}  {timing.avg_proof:>14}  {'N/A':>12}")
        p("  |  -> Run supply-chain e2e for full witness/proof breakdown")
    else:
        p("  |  (no per-proof timing data -- e2e may not output timing)")
    p("  |")
    p("  |  Proof Characteristics")
    p(f"  |  {'Proof size':<30}  {proof_size} bytes ({proof_size_desc})")
    p(f"  |  {'Public inputs (inst)':<30}  {l1.inst_inputs or zk.inst_inputs or '?'}")
    p(f"  |  {'Public inputs (trans)':<30}  {l1.trans_inputs or zk.trans_inputs or '?'}")
    p(f"  |  {'Public inputs (term)':<30}  {l1.term_inputs or zk.term_inputs or '?'}")
    p("  |")
    p("  |  Generated / Cached Artifacts")
    if artifacts.files:
        p(f"  |  {'Artifact':<22}  {'Size':>10}  Path")
        p(f"  |  {'-'*22}  {'-'*10}  {'-'*30}")
        for item in artifacts.files:
            try:
                path_display = str(item.path.relative_to(ROOT))
            except ValueError:
                path_display = str(item.path)
            p(f"  |  {item.label:<22}  {fmt_bytes_human(item.size_bytes):>10}  {path_display}")
        p(f"  |  {'TOTAL':<22}  {fmt_bytes_human(artifacts.total_bytes):>10}")
    else:
        p("  |  (no cached/generated artifact files found on disk)")
    p("  |")
    p("  +------------------------------------------------------------------+")
    p("")

    # E. Aggregates and Statistics
    p("  +-- E. Aggregates and Statistics ---------------------------------+")
    p("  |")
    if l1.ran and l1.gas_inst and zk.ran and zk.gas_inst:
        r_inst = safe_ratio(zk.gas_inst, l1.gas_inst)
        r_trans = safe_ratio(zk.gas_trans, l1.gas_trans)
        r_term = safe_ratio(zk.gas_term, l1.gas_term)
        r_tot = safe_ratio(zk.exec_total, l1.exec_total)

        p(f"  |  {'Operation':<20}  {'L1 (gas)':>14}  {'zkSync (gas)':>14}  {'Ratio':>8}")
        p(f"  |  {'-'*20}  {'-'*14}  {'-'*14}  {'-'*8}")
        p(f"  |  {'instantiate':<20}  {fmt_gas(l1.gas_inst):>14}  {fmt_gas(zk.gas_inst):>14}  {r_inst:>7}x")
        p(f"  |  {'transition':<20}  {fmt_gas(l1.gas_trans):>14}  {fmt_gas(zk.gas_trans):>14}  {r_trans:>7}x")
        p(f"  |  {'terminate':<20}  {fmt_gas(l1.gas_term):>14}  {fmt_gas(zk.gas_term):>14}  {r_term:>7}x")
        p(f"  |  {'-'*20}  {'-'*14}  {'-'*14}  {'-'*8}")
        p(f"  |  {'execution total':<20}  {fmt_gas(l1.exec_total):>14}  {fmt_gas(zk.exec_total):>14}  {r_tot:>7}x")

        if l1.deploy_total and zk.deploy_total:
            r_dep = safe_ratio(zk.deploy_total, l1.deploy_total)
            l1_grand = int(l1.deploy_total) + int(l1.exec_total or "0")
            zk_grand = int(zk.deploy_total) + int(zk.exec_total or "0")
            r_grand = safe_ratio(str(zk_grand), str(l1_grand))
            p(f"  |  {'deploy (one-time)':<20}  {fmt_gas(l1.deploy_total):>14}  {fmt_gas(zk.deploy_total):>14}  {r_dep:>7}x")
            p(f"  |  {'-'*20}  {'-'*14}  {'-'*14}  {'-'*8}")
            p(f"  |  {'deploy + one run':<20}  {fmt_gas(str(l1_grand)):>14}  {fmt_gas(str(zk_grand)):>14}  {r_grand:>7}x")

        p("  |")
        p(f"  |  Gas units: zkSync ~{r_inst}x higher per call (optimizer disabled on both)")
        p("  |  Note: gas units != cost; actual ETH fee = gasUsed x network gas price")

        # Helper: pick best gas price source for each chain
        l1_gp_gwei = nd.l1_gas_price_gwei
        zk_gp_wei = nd.zksync_max_fee_per_gas_wei  # from zks_estimateFee (precise)
        zk_gp_gwei = nd.zksync_gas_price_gwei       # fallback

        if l1_gp_gwei or zk_gp_wei or zk_gp_gwei:
            tc = zk.transition_count or l1.transition_count or "?"
            zk_src = "zks_estimateFee" if nd.zksync_est_inst else "eth_gasPrice"
            p("  |")
            p(f"  |  Estimated fees (L1: eth_gasPrice={l1_gp_gwei or 'N/A'} gwei, zkSync: {zk_src}={zk_gp_gwei or 'N/A'} gwei):")
            l1_exec_fee = calc_mainnet_fee_wei(l1.exec_total, gas_price_gwei=l1_gp_gwei)
            l1_deploy_fee = calc_mainnet_fee_wei(l1.deploy_total, gas_price_gwei=l1_gp_gwei)
            zk_exec_fee = calc_mainnet_fee_wei(zk.exec_total, gas_price_wei=zk_gp_wei, gas_price_gwei=zk_gp_gwei)
            zk_deploy_fee = calc_mainnet_fee_wei(zk.deploy_total, gas_price_wei=zk_gp_wei, gas_price_gwei=zk_gp_gwei)
            if l1_exec_fee:
                p(f"  |    L1 exec total     (1xinst + {tc}xtrans + 1xterm) : {l1_exec_fee}")
            if l1_deploy_fee:
                p(f"  |    L1 deploy total                            : {l1_deploy_fee}")
            if l1_exec_fee and l1_deploy_fee:
                l1_grand_fee = str(int(l1_exec_fee) + int(l1_deploy_fee))
                p(f"  |    L1 grand total (deploy + exec)            : {l1_grand_fee}")
            if zk_exec_fee:
                p(f"  |    ZK exec total     (1xinst + {tc}xtrans + 1xterm) : {zk_exec_fee}")
            if zk_deploy_fee:
                p(f"  |    ZK deploy total                            : {zk_deploy_fee}")
            if zk_exec_fee and zk_deploy_fee:
                zk_grand_fee = str(int(zk_exec_fee) + int(zk_deploy_fee))
                p(f"  |    ZK grand total (deploy + exec)            : {zk_grand_fee}")

        if nd.eth_usd and (l1_gp_gwei or zk_gp_wei or zk_gp_gwei):
            p("  |")
            p(f"  |  USD estimates from mainnet gas prices  (ETH=${nd.eth_usd})")
            p(f"  |  {'Operation':<20}  {'L1 (USD)':>14}  {'zkSync (USD)':>14}")
            p(f"  |  {'-'*20}  {'-'*14}  {'-'*14}")
            for op, l1g, zkg in [
                ("instantiate", l1.gas_inst, zk.gas_inst),
                ("transition", l1.gas_trans, zk.gas_trans),
                ("terminate", l1.gas_term, zk.gas_term),
            ]:
                l1_usd = calc_usd_gas(l1g, nd.eth_usd, gas_price_gwei=l1_gp_gwei)
                zk_usd = calc_usd_gas(zkg, nd.eth_usd, gas_price_wei=zk_gp_wei, gas_price_gwei=zk_gp_gwei)
                p(f"  |  {op:<20}  {l1_usd:>14}  {zk_usd:>14}")
            p(f"  |  {'-'*20}  {'-'*14}  {'-'*14}")
            l1_exec_usd = calc_usd_gas(l1.exec_total, nd.eth_usd, gas_price_gwei=l1_gp_gwei)
            zk_exec_usd = calc_usd_gas(zk.exec_total, nd.eth_usd, gas_price_wei=zk_gp_wei, gas_price_gwei=zk_gp_gwei)
            p(f"  |  {'execution total':<20}  {l1_exec_usd:>14}  {zk_exec_usd:>14}")
            if l1.deploy_total and zk.deploy_total:
                l1_dep_usd = calc_usd_gas(l1.deploy_total, nd.eth_usd, gas_price_gwei=l1_gp_gwei)
                zk_dep_usd = calc_usd_gas(zk.deploy_total, nd.eth_usd, gas_price_wei=zk_gp_wei, gas_price_gwei=zk_gp_gwei)
                p(f"  |  {'deploy (one-time)':<20}  {l1_dep_usd:>14}  {zk_dep_usd:>14}")
                l1_grand = str(int(l1.deploy_total) + int(l1.exec_total or "0"))
                zk_grand = str(int(zk.deploy_total) + int(zk.exec_total or "0"))
                p(f"  |  {'-'*20}  {'-'*14}  {'-'*14}")
                l1_grand_usd = calc_usd_gas(l1_grand, nd.eth_usd, gas_price_gwei=l1_gp_gwei)
                zk_grand_usd = calc_usd_gas(zk_grand, nd.eth_usd, gas_price_wei=zk_gp_wei, gas_price_gwei=zk_gp_gwei)
                p(f"  |  {'deploy + one run':<20}  {l1_grand_usd:>14}  {zk_grand_usd:>14}")

    elif l1.ran and l1.gas_inst:
        p(f"  |  {'Operation':<20}  {'L1 (gas)':>14}")
        p(f"  |  {'-'*20}  {'-'*14}")
        p(f"  |  {'instantiate':<20}  {fmt_gas(l1.gas_inst):>14}")
        p(f"  |  {'transition':<20}  {fmt_gas(l1.gas_trans):>14}")
        p(f"  |  {'terminate':<20}  {fmt_gas(l1.gas_term):>14}")
        p(f"  |  {'FULL RUN est.':<20}  {fmt_gas(l1.exec_total):>14}")
        p("  |")
        if zk.ran:
            p("  |  (zkSync gas not captured)")
        else:
            p("  |  (zkSync not run; anvil-zksync binary not found or port 8011 unavailable)")
    elif l1.ran:
        p("  |  L1 tests ran but no gas numbers were logged")
        p(f"  |  -> See: {L1_LOG}")
    else:
        p("  |  (gas tests were not run)")

    p("  |")
    p("  +------------------------------------------------------------------+")
    p("")

    # Log files
    p("  +-- Log Files ----------------------------------------------------+")
    p(f"  |  {'execution-service:':<22} {EXEC_LOG}")
    p(f"  |  {'bpmn-service:':<22} {BPMN_LOG}")
    if zksync_node_started:
        p(f"  |  {'anvil-zksync:':<22} {ANVIL_ZKSYNC_LOG}")
    if e2e_script:
        p(f"  |  {'e2e:':<22} {E2E_LOG}")
    if l1.ran:
        p(f"  |  {'L1 Hardhat:':<22} {L1_LOG}")
    if zk.ran:
        p(f"  |  {'zkSync Hardhat:':<22} {ZKSYNC_LOG}")
    p(f"  |  {'This report:':<22} {REPORT_PATH}")
    p("  +------------------------------------------------------------------+")
    p("")
    p(f"  Total wall time: {wall_time:.0f}s")
    p(SEP)

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline steps
# ═══════════════════════════════════════════════════════════════════════════════

def step_check_prerequisites():
    """Step 1: Verify required commands are available."""
    header("Step 1 | Prerequisites")
    for cmd in ["go", "node", "python3", "curl"]:
        path = shutil.which(cmd)
        if path:
            ok(f"{cmd}  ->  {path}")
        else:
            die(f"Required: {cmd}")


def step_free_ports():
    """Step 2: Free ports 3000, 8080, 8011."""
    header("Step 2 | Free ports 3000 / 8080 / 8011")
    for port in [3000, 8080, 8011]:
        free_port(port)


def step_proving_key_cache(clean: bool):
    """Step 3: Manage proving key cache."""
    header("Step 3 | Proving key cache")
    if clean:
        for pattern in ["*.constraint_system", "*.proving_key"]:
            for f in PUBLIC_DIR.glob(pattern):
                f.unlink()
        ok("Deleted cached keys (circuits will recompile on startup)")
    else:
        cached = len(list(PUBLIC_DIR.glob("*.proving_key")))
        if cached >= 3:
            ok(f"{cached} cached proving keys found  ->  fast startup")
        else:
            warn("No cached keys  ->  first-run compilation (adds ~22 s)")


def step_build(no_rebuild: bool):
    """Step 4-5: Build execution-service and install bpmn-service deps."""
    header("Step 4 | Build execution-service")
    if no_rebuild:
        ok("Skipping build (--no-rebuild)")
    else:
        build_execution_service()

    header("Step 5 | bpmn-service npm deps")
    if no_rebuild:
        ok("Skipping npm install (--no-rebuild)")
    else:
        install_bpmn_deps()


@dataclass
class RunningServices:
    exec_proc: Optional[subprocess.Popen] = None
    bpmn_proc: Optional[subprocess.Popen] = None
    zksync_proc: Optional[subprocess.Popen] = None

    @property
    def zksync_started(self) -> bool:
        return self.zksync_proc is not None


def step_start_services(skip_zksync: bool, skip_gas: bool, zksync_test: Optional[Path]) -> RunningServices:
    """Step 6-7: Start background services and wait for readiness."""
    svc = RunningServices()

    header("Step 6 | Start services")

    log("Starting execution-service (port 8080)...")
    svc.exec_proc = start_background(
        ["./execution-service"], EXEC_LOG, EXEC_DIR, "execution-service")

    log("Starting bpmn-service (port 3000)...")
    svc.bpmn_proc = start_background(
        ["npm", "run", "start"], BPMN_LOG, BPMN_DIR, "bpmn-service")

    if not skip_zksync and not skip_gas and zksync_test:
        zksync_bin = find_anvil_zksync()
        if zksync_bin:
            log("Starting anvil-zksync (port 8011)...")
            svc.zksync_proc = start_background(
                [str(zksync_bin), "--port=8011"], ANVIL_ZKSYNC_LOG, ROOT, "anvil-zksync")
        else:
            warn("anvil-zksync binary not found  ->  zkSync tests will be skipped")
            warn("Expected: ~/.cache/hardhat-nodejs/zksync-memory-node/<version>/anvil-zksync")

    header("Step 7 | Wait for services")
    wait_for_http("http://localhost:3000/", "bpmn-service (port 3000)", 120)
    wait_for_http("http://localhost:8080/publicKeys", "execution-service (port 8080)", 300)
    if svc.zksync_proc:
        wait_for_http("http://localhost:8011/", "anvil-zksync (port 8011)", 60)

    return svc


def step_run_e2e(e2e_script: Optional[Path]) -> tuple[bool, bool, float]:
    """Step 8: Run E2E script. Returns (ran, ok, duration_seconds)."""
    if not e2e_script:
        warn("No E2E script found -- skipping E2E phase")
        return False, False, 0.0

    header(f"Step 8 | E2E: {e2e_script.name}")
    t0 = time.monotonic()

    with open(E2E_LOG, "w") as log_fh:
        proc = subprocess.Popen(
            ["python3", str(e2e_script)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        # Stream output to both terminal and log file
        for line in proc.stdout:
            decoded = line.decode(errors="replace")
            sys.stdout.write(decoded)
            log_fh.write(decoded)
        proc.wait()

    duration = time.monotonic() - t0
    if proc.returncode == 0:
        ok(f"E2E completed in {duration:.0f}s")
        return True, True, duration
    else:
        warn(f"E2E exited with status {proc.returncode}  (see {E2E_LOG})")
        return True, False, duration


def step_run_gas_tests(
    skip_gas: bool,
    skip_zksync: bool,
    proofs_abs: Path,
    l1_test: Optional[Path],
    zksync_test: Optional[Path],
) -> tuple[GasMetrics, GasMetrics, NetworkData]:
    """Steps 10-11: Run L1 and zkSync gas tests. Returns (l1, zksync, network_data)."""
    l1 = GasMetrics()
    zk = GasMetrics()
    nd = NetworkData()

    if skip_gas:
        warn("Skipping gas tests (--skip-gas)")
        return l1, zk, nd

    if not proofs_abs.is_file():
        warn(f"Proofs file not found at {proofs_abs}")
        warn("Gas tests skipped (run the e2e first to generate proofs, or check --skip-gas)")
        return l1, zk, nd

    nd = fetch_network_data(proofs_abs)

    # L1 tests
    if not l1_test:
        warn("No L1 test file found  ->  L1 gas tests skipped")
    else:
        header(f"Step 10 | L1 Ethereum gas tests: {l1_test.name}")
        rel_path = str(l1_test.relative_to(SOL_DIR))
        with open(L1_LOG, "w") as log_fh:
            proc = subprocess.Popen(
                ["npx", "hardhat", "test", rel_path],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=SOL_DIR,
                env={**os.environ, "REPORT_GAS": "true"},
            )
            for line in proc.stdout:
                decoded = line.decode(errors="replace")
                sys.stdout.write(decoded)
                log_fh.write(decoded)
            proc.wait()

        if proc.returncode != 0:
            warn(f"L1 tests had failures (see {L1_LOG})")

        l1 = parse_l1_gas(L1_LOG)
        if l1.gas_inst:
            ok(f"L1 gas: instantiate={fmt_gas(l1.gas_inst)}  transition={fmt_gas(l1.gas_trans)}  terminate={fmt_gas(l1.gas_term)}")
        else:
            warn(f"L1 gas numbers not found in output (test may not log gas -- see {L1_LOG})")

    # zkSync tests
    if skip_zksync:
        warn("Skipping zkSync tests (--skip-zksync)")
        return l1, zk, nd

    if not zksync_test:
        warn("No zkSync test file found  ->  zkSync gas tests skipped")
        return l1, zk, nd

    # Check port 8011
    import socket
    try:
        s = socket.create_connection(("localhost", 8011), timeout=2)
        s.close()
    except (ConnectionRefusedError, OSError):
        warn("anvil-zksync not reachable on port 8011  ->  zkSync tests skipped")
        return l1, zk, nd

    header(f"Step 11 | zkSync ERA gas tests: {zksync_test.name}")
    env = {**os.environ, "PATH": f"{NODE23_BIN}:{os.environ.get('PATH', '')}"}

    log("Compiling for zkSync ERA...")
    subprocess.run(
        ["npx", "hardhat", "compile", "--network", "zkSyncLocalNode"],
        cwd=SOL_DIR, env=env, capture_output=True,
    )

    log("Running zkSync tests...")
    rel_path = str(zksync_test.relative_to(SOL_DIR))
    with open(ZKSYNC_LOG, "w") as log_fh:
        proc = subprocess.Popen(
            ["npx", "hardhat", "test", rel_path, "--network", "zkSyncLocalNode"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=SOL_DIR, env=env,
        )
        for line in proc.stdout:
            decoded = line.decode(errors="replace")
            sys.stdout.write(decoded)
            log_fh.write(decoded)
        proc.wait()

    if proc.returncode != 0:
        warn(f"zkSync tests had failures (see {ZKSYNC_LOG})")

    zk = parse_zksync_gas(ZKSYNC_LOG)
    if zk.gas_inst:
        ok(f"zkSync gas: instantiate={fmt_gas(zk.gas_inst)}  transition={fmt_gas(zk.gas_trans)}  terminate={fmt_gas(zk.gas_term)}")
    else:
        warn(f"zkSync gas numbers not found (see {ZKSYNC_LOG})")

    return l1, zk, nd


# ═══════════════════════════════════════════════════════════════════════════════
# Argument parsing
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ZK Choreography Runner — end-to-end ZK proof generation and gas benchmarking",
    )
    parser.add_argument("bpmn_file", metavar="bpmn-file", help="Path to BPMN file (relative to project root)")
    parser.add_argument("--e2e", dest="e2e_script", help="E2E Python script to run")
    parser.add_argument("--l1-test", dest="l1_test", help="L1 Ethereum Hardhat test file")
    parser.add_argument("--zksync-test", dest="zksync_test", help="zkSync ERA Hardhat test file")
    parser.add_argument("--name", dest="display_name", help="Override display name")
    parser.add_argument("--clean", action="store_true", help="Delete cached proving keys (force recompile)")
    parser.add_argument("--skip-zksync", action="store_true", help="Skip zkSync ERA gas tests")
    parser.add_argument("--skip-gas", action="store_true", help="Skip all Hardhat gas tests")
    parser.add_argument("--no-rebuild", action="store_true", help="Skip go build and npm install")
    return parser.parse_args()


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    args = parse_args()
    t_start = time.monotonic()

    # Resolve BPMN file
    bpmn_path = Path(args.bpmn_file)
    if not bpmn_path.is_absolute():
        bpmn_path = ROOT / bpmn_path
    if not bpmn_path.is_file():
        die(f"BPMN file not found: {bpmn_path}")

    # Derive names
    basename = bpmn_path.stem  # e.g. "supply-chain"
    snake = to_snake(basename)
    pascal = to_pascal(basename)
    display_name = args.display_name or to_display(basename)

    # Auto-discover components (override -> auto-discover -> None)
    e2e_script = Path(args.e2e_script) if args.e2e_script else discover_e2e(snake)
    l1_test = Path(args.l1_test) if args.l1_test else discover_l1_test(pascal)
    zksync_test = Path(args.zksync_test) if args.zksync_test else discover_zksync_test(pascal)

    # Detect proofs JSON
    if l1_test:
        proofs_rel = detect_proofs_json(l1_test) or "test/proofs.json"
    else:
        proofs_rel = "test/proofs.json"
    proofs_abs = SOL_DIR / proofs_rel

    # Banner
    print()
    print(f"{BOLD}{CYAN}========================================================{RESET}")
    print(f"{BOLD}{CYAN}  ZK CHOREOGRAPHY RUNNER{RESET}")
    print(f"{BOLD}{CYAN}========================================================{RESET}")
    print()
    info(f"BPMN file   : {args.bpmn_file}")
    info(f"Name        : {display_name}")
    info(f"Snake name  : {snake}")
    info(f"Pascal name : {pascal}")
    print()
    info("Auto-discovered components:")
    if e2e_script:
        info(f"  E2E script   : {e2e_script}")
    else:
        warn("  E2E script   : NOT FOUND (no e2e will run)")
    if l1_test:
        info(f"  L1 test      : {l1_test}")
    else:
        warn("  L1 test      : NOT FOUND (L1 gas tests skipped)")
    if zksync_test:
        info(f"  zkSync test  : {zksync_test}")
    else:
        warn("  zkSync test  : NOT FOUND (zkSync gas tests skipped)")
    info(f"  Proofs JSON  : {proofs_rel} (expected at {proofs_abs})")
    print()

    # Run pipeline
    step_check_prerequisites()
    step_free_ports()
    step_proving_key_cache(args.clean)
    step_build(args.no_rebuild)
    svc = step_start_services(args.skip_zksync, args.skip_gas, zksync_test)

    e2e_ran, e2e_ok, e2e_duration = step_run_e2e(e2e_script)

    # ZK timing metrics — four phases:
    #   1. Compile: circuit -> R1CS (one-time, cached)
    #   2. Setup:   R1CS -> proving/verification keys (one-time, cached)
    #   3. Witness: inputs -> witness assignment (per proof, negligible)
    #   4. Proof:   witness + pk -> Groth16 proof (per proof, dominant cost)
    header("Step 9 | ZK timing metrics")
    timing = parse_zk_timing(EXEC_LOG, E2E_LOG)

    if timing.constraints_inst:
        ok(f"Constraints: inst={fmt_gas(timing.constraints_inst)}  trans={fmt_gas(timing.constraints_trans)}  term={fmt_gas(timing.constraints_term)}")

    info("[1] Compile  (one-time: circuit definition -> R1CS constraint system)")
    info("[2] Setup    (one-time: R1CS -> proving key + verification key)")
    if timing.compile_inst:
        ok(f"Compile: inst={timing.compile_inst}ms  trans={timing.compile_trans}ms  term={timing.compile_term}ms")
        ok(f"Setup:   inst={timing.setup_inst}ms  trans={timing.setup_trans}ms  term={timing.setup_term}ms")
    else:
        ok("Compile/Setup: keys were cached on disk (use --clean to recompile)")

    info("[3] Witness  (per proof: construct public+private inputs, ~0.2-0.3ms)")
    info("[4] Proof    (per proof: Groth16 prover, dominant cost ~950-1460ms)")
    if timing.inst_proof:
        ok(f"Witness: inst={timing.inst_witness}ms  trans(avg)={timing.avg_witness}ms  term={timing.term_witness}ms")
        ok(f"Proof:   inst={timing.inst_proof}ms  trans(avg)={timing.avg_proof}ms  term={timing.term_proof}ms")
        ok(f"Total:   inst={timing.inst_total}s  trans(avg)={timing.avg_total}s  term={timing.term_total}s")
    elif timing.avg_proof:
        ok(f"Avg proof time: {timing.avg_proof}ms")
    else:
        warn("Witness/Proof timing: not available from e2e output")

    artifacts = collect_artifact_metrics(proofs_abs)
    if artifacts.files:
        ok(f"Artifacts on disk: {len(artifacts.files)} files  total={fmt_bytes_human(artifacts.total_bytes)}")
        for item in artifacts.files:
            try:
                rel_path = item.path.relative_to(ROOT)
            except ValueError:
                rel_path = item.path
            info(f"  {item.label:<18} {fmt_bytes_human(item.size_bytes):>10}  {rel_path}")
    else:
        warn("No cached/generated artifact files found on disk")

    # Gas tests
    l1, zk, nd = step_run_gas_tests(args.skip_gas, args.skip_zksync, proofs_abs, l1_test, zksync_test)

    # Metadata
    meta = collect_metadata()

    # Final report
    wall_time = time.monotonic() - t_start
    header("Step 12 | Final Report")

    report = generate_report(
        display_name=display_name,
        bpmn_arg=args.bpmn_file,
        meta=meta,
        timing=timing,
        artifacts=artifacts,
        l1=l1,
        zk=zk,
        nd=nd,
        e2e_ran=e2e_ran,
        zksync_node_started=svc.zksync_started,
        e2e_script=e2e_script,
        wall_time=wall_time,
    )

    print(report)
    REPORT_PATH.write_text(report)
    print()
    print(f"  {GREEN}{BOLD}Report saved -> {REPORT_PATH}{RESET}")
    print()


if __name__ == "__main__":
    main()
