"""身份与落盘工具（草稿，未封存）。只用标准库，Mac 的 `python3 -I` 和台式机的任何虚拟环境都能导入。

- bridge 代码身份：bridge/ 顶层 *.py 与 *.json（含冻结的公开配置），以及 agent 环境锁文件 requirements-mini.lock.txt
  （补记 2，审核 I3），文件名加字节，按文件名排序后求 sha256；不含 tests/、tools/、README，也不含价目登记表 pricing_registry.json（v4：登记表可能在运行中途由作者补登新版本串，
  它的 sha256 另记进每条轨迹的运行记录与账本，见 registry_sha256()）。
- d1 代码身份：与 d1/run_task.py 的 code_identity() 同一公式（d1/*.py 的文件名加字节）。这里只重算，不导入、
  不改 d1；封存值写死在 D1_EXPECTED，运行与分析前都要核对，不一致就停（fail-closed）。
"""
import glob, hashlib, json, os, time

HERE = os.path.dirname(os.path.abspath(__file__))
D1_DIR = os.path.normpath(os.path.join(HERE, "..", "d1"))
D1_EXPECTED = "ce9fe5e4a6282975e696eb2ecdd7be113d7b1464a5acee1d087bee52109593c3"
K = 50
COST_LIMIT = 3.0


def _tree_hash(paths):
    h = hashlib.sha256()
    for p in sorted(paths, key=os.path.basename):
        h.update(os.path.basename(p).encode()); h.update(open(p, "rb").read())
    return h.hexdigest()


REGISTRY = "pricing_registry.json"
ENV_LOCK = "requirements-mini.lock.txt"      # addendum 2 (review I3): the agent environment lock is part of the identity


def bridge_code_sha256(root=HERE):
    files = glob.glob(os.path.join(root, "*.py")) + glob.glob(os.path.join(root, "*.json"))
    files = [p for p in files if os.path.basename(p) != REGISTRY]
    if os.path.exists(os.path.join(root, ENV_LOCK)):
        files.append(os.path.join(root, ENV_LOCK))
    return _tree_hash(files)


def env_lock_pins(path=None):
    """{normalised name: version} from the frozen agent-environment lock (lines 'name==version')."""
    out = {}
    for line in open(path or os.path.join(HERE, ENV_LOCK), encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            n, v = line.split("==", 1)
            out[n.strip().lower().replace("_", "-")] = v.strip()
    return out


def load_registry(path=None):
    path = path or os.path.join(HERE, REGISTRY)
    raw = open(path, "rb").read()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def d1_code_sha256(root=D1_DIR):
    return _tree_hash(glob.glob(os.path.join(root, "*.py")))


def check_d1(expected=D1_EXPECTED):
    got = d1_code_sha256()
    if got != expected:
        raise SystemExit(f"d1 code identity mismatch: got {got}, sealed {expected}")
    return got


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def atomic_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, default=str); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_bytes(path, data: bytes):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def ledger(path, obj):
    """追加一行到 jsonl 账本（只追加，不改写）。"""
    with open(path, "a") as f:
        f.write(json.dumps(dict(obj, t=obj.get("t") or now()), default=str) + "\n"); f.flush(); os.fsync(f.fileno())


def image_name(instance_id):
    """与 run_one 给 DockerEnvironment 用的镜像名一致。"""
    return "docker.io/swebench/sweb.eval.x86_64." + instance_id.replace("__", "_1776_").lower() + ":latest"


def no_bytecode_in_d1():
    """导入 d1 模块前调用：不往 d1/__pycache__ 写任何东西（d1 是封存目录）。"""
    import sys
    sys.dont_write_bytecode = True


def import_d1(*names):
    """按名导入已封存的 d1 模块（只读）。d1 与 bridge 有同名文件（batch.py），所以不把 d1 长期留在 sys.path 上：
    临时插到最前面导入，再移除；judge.judge() 运行时会 `from metrics import ...`，因此 metrics 一并预先导入。
    每个模块都核对确实来自 d1/，否则停（fail-closed）。"""
    import importlib, sys
    no_bytecode_in_d1()
    sys.path.insert(0, D1_DIR)
    try:
        mods = [importlib.import_module(n) for n in ("metrics",) + tuple(names)]
    finally:
        sys.path.remove(D1_DIR)
    for m in mods:
        if os.path.dirname(os.path.abspath(m.__file__)) != D1_DIR:
            raise SystemExit(f"module {m.__name__} resolved to {m.__file__}, not the sealed d1/")
    return mods[1:] if len(mods) > 2 else mods[1]


class OutLock:
    """Exclusive, non-blocking lock on OUT/batch.lock (review I4). Held by a batch for its whole run and by every
    author maintenance command (void/ack/re-evaluate/clear/rebuild), so two writers can never act on one output dir."""

    def __init__(self, out):
        self.path = os.path.join(os.path.abspath(out), "batch.lock"); self.fh = None

    def __enter__(self):
        import fcntl
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close(); self.fh = None
            raise SystemExit(f"{self.path} is held by another process (a batch or a maintenance command is running)")
        self.fh.seek(0); self.fh.truncate(); self.fh.write(f"{os.getpid()} {now()}\n"); self.fh.flush()
        return self

    def __exit__(self, *exc):
        import fcntl
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN); self.fh.close(); self.fh = None
