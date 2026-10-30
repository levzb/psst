#!/usr/bin/env python3
"""psst: describe a task in plain words, get one exact shell command for this machine and terminal session.

    psst find files bigger than 1 MB      # the command lands in your input line, it never runs by itself
    psst                                  # fix the previous command
    psst --key [KEY]                      # save your OpenRouter key (asked for, hidden, if KEY is left out)
    psst --setup [-y] | --uninstall       # add or remove the shell integration (zsh, bash, fish, PowerShell)
    psst --init zsh|bash|fish|pwsh        # print the integration code
    psst --selftest | --eval [1-12,15]    # offline tests | live model on fixtures in a temp dir
Needs an OpenRouter key (free: https://openrouter.ai/keys), from `psst --key` or OPENROUTER_API_KEY.
"""
import datetime, functools, hashlib, json, os, platform, re, shlex, shutil, sqlite3, subprocess, sys, tempfile, threading, time
import urllib.error, urllib.request
from pathlib import Path

VERSION = "0.1.2"
API = "https://openrouter.ai/api/v1"
WIN = os.name == "nt"
SHELLS = {  # name: (run a script, check syntax of stdin, list builtins)
    "zsh": (["-fc"], ["-n", "-f"], "print -l ${(k)builtins} ${(k)reswords}"),
    "bash": (["-c"], ["-n"], "compgen -b; compgen -k"),
    "fish": (["-c"], ["-n"], "builtin -n"),
    "pwsh": (["-NoProfile", "-NonInteractive", "-Command"], None, "Get-Command -CommandType Cmdlet,Function | ForEach-Object Name"),
}
SHELLS["powershell"] = SHELLS["pwsh"]
# PowerShell's own parser: the first syntax error, or one line per command with its words tab-separated ('$' for non-literals)
PS_PARSE = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; $e = $null; "
            "$a = [Management.Automation.Language.Parser]::ParseInput($env:PSST_CHECK, [ref]$null, [ref]$e); if ($e) { $e[0].Message; exit 1 }; "
            "$a.FindAll({ $args[0] -is [Management.Automation.Language.CommandAst] }, $true) | ForEach-Object { ($_.CommandElements | ForEach-Object { "
            "if ($_ -is [Management.Automation.Language.StringConstantExpressionAst]) { $_.Value -replace '\\s', ' ' } "
            "elseif ($_ -is [Management.Automation.Language.CommandParameterAst]) { '-' + $_.ParameterName } else { '$' } }) -join \"`t\" }")
BEGIN, END = "# >>> psst >>>", "# <<< psst <<<"
MODELS = ["cohere/north-mini-code:free", "xingchen-agi/xing4.0-29b-a4b:free", "google/gemma-4-31b-it:free"]
KEY_UTILS = "sed grep find awk date stat tar xargs ls cp mv rm du df sort head tail ps readlink base64 split touch cut tac timeout nproc".split()
PKG = "brew port nix apt dnf yum pacman zypper apk snap flatpak pip pip3 pipx uv conda mamba npm pnpm yarn bun cargo gem go".split()
KEYWORDS = {"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "time", "esac", "coproc", "and", "or", "not", "begin", "end"}
SKIPSEG = {"for", "select", "case", "function", "foreach", "repeat", "[["}
WRAPPERS = {"sudo", "doas", "env", "nohup", "nice", "time", "xargs", "exec", "caffeinate", "timeout", "gtimeout", "watch", "stdbuf", "noglob", "nocorrect", "command", "builtin"}
NO_H = {"shutdown", "halt", "reboot", "poweroff"}  # never probe these with -h
ASSIGN = re.compile(r"[A-Za-z_]\w*(\[[^\]]*\])?\+?=")
INSTALL = re.compile(r"\b(brew|port|apt|apt-get|dnf|yum|zypper|apk|pip3?|pipx|uv|npm|pnpm|yarn|bun|cargo|gem|go)\s+(install|add|i)\b|\bpacman\s+-S")
DANGER = re.compile(r"\brm\s+(-\S*[rRf]|--(recursive|force))|\bdd\b|\bmkfs|\bsudo\b|\bdoas\b|\bchmod\s+(-\S*R|--recursive)|\bchown\s+-\S*R|(?<![>&\d])>(?![>&]|\s*/dev/null)")
RISKS = ("read", "write", "delete", "network", "sudo")
STATS = {"requests": 0, "model": "", "gap": 0.0, "last": 0.0}

EXAMPLES = [  # (task, candidate commands...): the model sees the first candidate that passes validation on this machine
    ("delete lines containing DEBUG from app.log in place", "sed -i '/DEBUG/d' app.log", "sed -i '' '/DEBUG/d' app.log"),
    ("remove leading whitespace from every line of x.txt in place", "sed -i -E 's/^\\s+//' x.txt", "sed -i '' -E 's/^[[:space:]]+//' x.txt"),
    ("date one week ago as YYYY-MM-DD", "date -d '7 days ago' +%F", "date -v-7d +%F", "(Get-Date).AddDays(-7).ToString('yyyy-MM-dd')"),
    ("unix time 1700000000 as a readable UTC date", "date -u -d @1700000000 '+%F %T'", "date -u -r 1700000000 '+%F %T'"),
    ("size of report.pdf in bytes", "stat -c %s report.pdf", "stat -f %z report.pdf", "(Get-Item report.pdf).Length"),
    ("owner and octal permissions of each file here", "stat -c '%U %a %n' *", "stat -f '%Su %Lp %N' *"),
    ("every .csv under data/ with its modification time", "find data -name '*.csv' -printf '%TY-%Tm-%Td %TH:%TM %p\\n'",
     "find data -name '*.csv' -exec stat -f '%Sm %N' {} +"),
    ("print notes.txt last line first", "tac notes.txt", "tail -r notes.txt"),
    ("print the numbers that follow 'port=' in cfg.ini", "grep -oP '(?<=port=)\\d+' cfg.ini", "grep -o 'port=[0-9]*' cfg.ini | cut -d= -f2"),
    ("number of CPU cores", "nproc", "sysctl -n hw.ncpu", "getconf _NPROCESSORS_ONLN"),
    ("stop ./job.sh if it runs longer than 5 seconds", "timeout 5 ./job.sh", "perl -e 'alarm 5; exec @ARGV' ./job.sh"),
    ("what is listening on port 8080", "ss -ltnp 'sport = :8080'", "lsof -nP -iTCP:8080 -sTCP:LISTEN"),
    ("copy the contents of key.pub to the clipboard", "xclip -selection clipboard < key.pub", "wl-copy < key.pub", "pbcopy < key.pub"),
    ("make backup.tgz of project/ without node_modules", "tar -czf backup.tgz --exclude=node_modules project"),
    ("the 5 biggest directories here", "du -sh */ | sort -rh | head -n 5"),
    ("delete .log files older than 30 days, names may contain spaces", "find . -name '*.log' -mtime +30 -delete",
     "Get-ChildItem -Recurse -Filter *.log | Where-Object LastWriteTime -lt (Get-Date).AddDays(-30) | Remove-Item"),
    ("rename every .txt here to .md", "for f in *.txt; do mv -- \"$f\" \"${f%.txt}.md\"; done"),
    ("install requests into the active virtualenv", "python -m pip install requests", "python3 -m pip install requests"),
    ("fix my previous command `gti status` (exit status 127)", "git status"),
]

SYSTEM = """You write exactly one shell command for the machine and terminal session described below. The user describes a task in plain words, in any language.
Reply with one JSON object and nothing else:
{"command": "<one line for this shell>", "utilities": ["<every program the command runs>"], "explanation": "<one short line, written in the same human language as the task>", "risk": "read|write|delete|network|sudo"}
Rules:
- The command must work here as is: use only installed commands listed below, or this session's aliases, functions and shell builtins.
- Every flag must exist in the listed variant of its utility. BSD/macOS tools are not GNU tools: never use GNU-only flags or regex syntax (\\s, \\+, -P) with them. In basic regex (sed, grep without -E) + ? | ( ) are literal: use -E for them.
- Work relative to the current directory unless the task names other paths. Use only file names from the task or the directory listing. Quote names; handle spaces.
- No sudo unless the task needs root. No interactive editors or pagers. Prefer one simple pipeline over scripts.
- An active virtualenv or conda env already puts its python and pip first on PATH.
- risk is the most dangerous effect of the command: sudo > delete > write > network > read.
- The explanation uses the human language of the task text itself, whatever language that is.

Session:
{session}

Examples for this machine (task -> command):
{examples}"""


# ---------- cache: one sqlite file, every entry carries the fingerprint it was computed from ----------

_db = None


def db_dir():
    return Path(os.environ.get("XDG_CACHE_HOME") or WIN and os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "psst"


def db():
    global _db
    if _db is None:
        d = db_dir()
        d.mkdir(parents=True, exist_ok=True)
        _db = sqlite3.connect(d / "cache.sqlite3", isolation_level=None, timeout=5)
        _db.execute("create table if not exists kv (k text primary key, v text)")
    return _db


def kv_get(k):
    row = db().execute("select v from kv where k = ?", (k,)).fetchone()
    return json.loads(row[0]) if row else None


def kv_put(k, fp, val):
    db().execute("insert or replace into kv values (?, ?)", (k, json.dumps({"fp": fp, "val": val})))


def cached(k, fp, make):
    hit = kv_get(k)
    if hit and hit["fp"] == fp:
        return hit["val"]
    val = make()
    kv_put(k, fp, val)
    return val


def stamp(p):
    try:
        st = os.stat(p)
        return [os.path.realpath(p), st.st_mtime_ns, st.st_size]
    except OSError:
        return None


def run(argv, timeout=3, limit=300_000, cwd=None):
    """Run a probe with no stdin and no terminal; cap its time and output."""
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, cwd=cwd)
    except OSError:
        return ""
    kill = lambda: (p.kill() if WIN else os.killpg(p.pid, 9)) if p.poll() is None else None
    t = threading.Timer(timeout, kill)
    t.start()
    out = p.stdout.read(limit)
    t.cancel()
    try:
        kill()
    except OSError:
        pass
    p.wait()
    return re.sub(".\b", "", out.decode(errors="replace"))


# ---------- session snapshot ----------

def dir_cmds(d):
    try:
        mtime = os.stat(d).st_mtime_ns
    except OSError:
        return []
    exts = os.environ.get("PATHEXT", ".EXE;.CMD;.BAT").lower().split(";") if WIN else None

    def make():
        names = []
        for e in os.scandir(d):
            try:
                if e.is_file() and (os.path.splitext(e.name)[1].lower() in exts if WIN else os.access(e.path, os.X_OK)):
                    names.append(e.name)
            except OSError:
                pass
        return sorted(names)
    return cached("dir\0" + d, mtime, make)


def parse_aliases(text):
    out = {}
    for line in text.splitlines():
        name, eq, val = re.sub(r"^alias ", "", line).partition("=")
        if eq:
            try:
                out[name] = " ".join(shlex.split(val))
            except ValueError:
                out[name] = val
    return out


def detect_shell():
    """The shell psst was started from, for the simplified mode without integration."""
    if WIN:
        return "pwsh" if shutil.which("pwsh") else "powershell"
    def ps(pid, field):  # Linux /proc first: slim containers have no ps
        try:
            return open(f"/proc/{pid}/comm").read().strip() if field == "comm" else open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[1]
        except (OSError, IndexError):
            return run(["ps", "-o", field + "=", "-p", str(pid)]).strip()
    for pid in (os.getppid(), ps(os.getppid(), "ppid")):  # parent, or grandparent behind a launcher
        name = os.path.basename(ps(pid, "comm")).lstrip("-")
        if name in SHELLS:
            return name
    return os.path.basename(os.environ.get("SHELL", "sh"))


def session(env=None, cwd=None):
    env = os.environ if env is None else env
    sh = env.get("PSST_SHELL") or detect_shell()
    hist = [l.strip() for l in env.get("PSST_LAST", "").splitlines() if l.strip()]
    if hist and re.match(r"psst(\s|$)", hist[-1]):
        hist.pop()  # the psst invocation itself
    s = {"shell": sh, "shell_path": env.get("PSST_SHELL_PATH") or shutil.which(sh, path=env.get("PATH")) or sh,
         "shell_version": env.get("PSST_SHELL_VERSION", ""), "path": env.get("PATH", ""), "cwd": cwd or os.getcwd(),
         "venv": env.get("VIRTUAL_ENV", ""), "conda": env.get("CONDA_DEFAULT_ENV", ""), "aliases": parse_aliases(env.get("PSST_ALIASES", "")),
         "funcs": set(env.get("PSST_FUNCS", "").split()), "last": hist[-1] if hist else "", "status": env.get("PSST_STATUS", ""), "cmds": {}}
    for d in s["path"].split(os.pathsep):
        for n in dir_cmds(d):
            for name in {n, os.path.splitext(n)[0]} if WIN else (n,):
                s["cmds"].setdefault(name, os.path.join(d, n))
    run_script, _, script = SHELLS.get(sh, SHELLS["bash"])
    s["builtins"] = set(cached("builtins\0" + sh, stamp(s["shell_path"]), lambda: run([s["shell_path"]] + run_script + [script], timeout=10).split()))
    return s


def resolve(w, s):
    if "/" in w or WIN and "\\" in w:
        p = os.path.join(s["cwd"], os.path.expanduser(w))
        return ("file", p) if os.path.isfile(p) and os.access(p, os.X_OK) else (None, None)
    if w in s["aliases"]:
        return "alias", s["aliases"][w]
    if w in s["funcs"]:
        return "function", None
    if w in s["builtins"]:
        return "builtin", None
    if w in s["cmds"]:
        return "file", s["cmds"][w]
    return None, None


def fingerprint(w, s):
    kind, info = resolve(w, s)
    return stamp(info) if kind == "file" else [kind, info] if kind else None


def variant(path):
    """(kind, first line of --version) for a binary; kind is GNU, BSD, BusyBox, uutils or other."""
    def make():
        v = run([path, "--version"], timeout=2).strip()
        low, first = v.lower(), (v.splitlines() or [""])[0][:60]
        bad = re.search(r"illegal option|unrecognized option|invalid option|usage:", low)
        kind = ("BusyBox" if "busybox" in low or os.path.basename(os.path.realpath(path)) == "busybox" else "uutils" if "uutils" in low
                else "BSD" if re.search(r"bsd|apple|darwin", low) else "GNU" if "gnu" in low or "free software foundation" in low
                else "BSD" if bad else "other")
        return [kind, "" if bad else first]
    return cached("ver\0" + path, stamp(path), make)


def utility_variants(s):
    out = []
    for u in KEY_UTILS:
        kind, info = resolve(u, s)
        if kind == "file":
            v, ver = variant(info)
            out.append(f"{u}={v}" + (f" ({ver})" if ver and v != "BSD" or u in ("grep", "awk", "tar", "sort") and ver else ""))
        else:
            out.append(f"{u}={kind or 'missing'}" + (f" '{info}'" if kind == "alias" else ""))
    return ", ".join(out)


def snapshot(s):
    sysname = platform.system()
    if sysname == "Darwin":
        osname = f"macOS {platform.mac_ver()[0]} {platform.machine()}"
    elif WIN:
        osname = f"Windows {platform.release()} ({platform.version()}) {platform.machine()}"
    else:
        rel = Path("/etc/os-release").read_text() if Path("/etc/os-release").exists() else ""
        osname = (re.findall(r'PRETTY_NAME="?([^"\n]*)', rel) or [f"{sysname} {platform.release()}"])[0] + f" {platform.machine()}"
    try:
        entries = sorted(e.name + ("/" if e.is_dir() else "") for e in os.scandir(s["cwd"]))
    except OSError:
        entries = []
    files = " ".join(entries[:50]) + (f" (+{len(entries) - 50} more)" if len(entries) > 50 else "")
    lines = [f"OS: {osname}", f"Shell: {s['shell']} {s['shell_version']}".rstrip(), f"Current directory: {s['cwd']}",
             f"Files here: {files or '(empty)'}",
             f"Active virtualenv: {s['venv'] or 'none'}" + (f"; conda env: {s['conda']}" if s["conda"] else ""),
             f"Last command: {s['last']!r} (exit status {s['status'] or '?'})" if s["last"] else "Last command: none",
             "Package managers: " + (", ".join(p for p in PKG if p in s["cmds"]) or "none"),
             "Utility variants: " + utility_variants(s),
             "Aliases: " + ("; ".join(f"{k}='{v}'" for k, v in s["aliases"].items()) or "none"),
             "Functions: " + (" ".join(sorted(f for f in s["funcs"] if not f.startswith(("_", "+")))) or "none"),
             f"PATH: {s['path']}", "Installed commands: " + " ".join(sorted(s["cmds"]))]
    return "\n".join(lines)


def example_forms(s):
    """[(task, first candidate that validates here, rejected candidates)], cached on the programs the candidates name"""
    def make():
        out = []
        for task, *cands in EXAMPLES:
            n = next((k for k, c in enumerate(cands) if not (lambda r: r["missing"] or r["warn"])(validate(c, s, shell_check=False))), None)
            if n is not None:
                out.append((task, cands[n], cands[:n]))
        return out
    words = sorted({w for _, *cands in EXAMPLES for c in cands for w in re.findall(r"[\w.-]+", c)})
    return cached("examples\0" + s["shell"], [fingerprint(w, s) for w in words], make)


def examples(s):
    return "\n".join(f"{task} -> {cmd}" for task, cmd, _ in example_forms(s))


# ---------- validation against the real binaries ----------

def helptext(path, name, sub=""):
    def make():
        try:
            with open(path, "rb") as f:
                head = f.read(65536)
        except OSError:
            head = b""
        probe = not head.startswith(b"#!") or re.search(rb"help|usage|exec |sys\.exit|require\(", head, re.I)
        base = [path] + ([sub] if sub else [])
        with tempfile.TemporaryDirectory() as empty:
            out = run(base + ["--help"], cwd=empty) if probe else ""
            if probe and len(out) < 300 and name not in NO_H:
                out += "\n" + run(base + ["-h"], cwd=empty)
        return out + "\n" + run(["man", "-P", "cat", f"{name}-{sub}" if sub else name], timeout=5)
    return cached(f"help\0{path}\0{sub}", stamp(path), make).replace("\u2010", "-").replace("\u2212", "-")


@functools.lru_cache(maxsize=256)
def flagset(h):
    """Flags that the help text defines: option lines (`-x, --long=ARG`) and usage brackets (`[-abc] [-f file]`)."""
    flags, req, opt = set(), set(), set()

    def add(f, rest):
        flags.add(f)
        if rest.startswith(("[", "=[")):
            opt.add(f)
        elif rest:
            req.add(f)
    arg = r"((?:=|\[)\S*| [<\[A-Za-z]\S*)?"
    for m in re.finditer(r"\[ ?-([\w@%#,]+) ?\]", h):
        flags.update("-" + c for c in m.group(1))
    for m in re.finditer(r"\[ ?(--?[\w?][\w-]*)" + arg, h):
        add(m.group(1), (m.group(2) or "").strip())
    for line in h.splitlines():
        if line.lstrip().startswith("-"):
            ms = [(m.group(1), (m.group(2) or "").strip()) for m in
                  re.finditer(r"(?<![\w-])(--?[\w?@#%][\w-]*)" + arg, re.split(r"\s{2,}|\t", line.strip())[0])]
            shared = next((r for _, r in ms if r), "")  # `-k, --key=KEYDEF`, `-k, --key <key>`: -k takes it too
            for f, rest in ms:
                add(f, rest or shared)
    flags.update(re.findall(r"(?<![\w-])(--[A-Za-z0-9][\w-]+)", h))
    return flags, req, opt


def flag_ok(a, F):
    """(known, takes the next token as its value)"""
    flags, req, opt = F
    if a.startswith("--"):
        name, eq, _ = a.partition("=")
        m = [name] if name in flags else [f for f in flags if f.startswith(name) and len(name) > 3]
        return bool(m), bool(m) and not eq and m[0] in req
    if re.match(r"-\d", a) or a.split("=")[0] in flags and len(a) > 2:
        return True, False
    for k, c in enumerate(a[1:]):
        f = "-" + c
        if f not in flags:
            return False, False
        if f in req or f in opt:
            return True, k == len(a) - 2 and f in req
    return True, False


def check_flags(path, name, args, res):
    h = helptext(path, name)
    if len(h.strip()) < 20:
        return  # no help: abstain
    F, sub, i = flagset(h), "", 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if name == "find" and a in ("-exec", "-execdir", "-ok", "-okdir"):
            j = next((k for k in range(i + 1, len(args)) if args[k] in ("\0", ";", "+")), len(args))
            check_cmd(args[i + 1:j], res)
            i = j + 1
            continue
        if a.startswith("-") and len(a) > 1 and not (name == "find" and a in ("-o", "-a", "-or", "-and", "-not")):
            ok, eat = flag_ok(a, F)
            if not ok and not a.startswith("--") and not any(re.fullmatch(r"-\w", f) for f in F[0]):
                ok = True  # the help lists no short flags at all (dash, `ps --help` without man pages): abstain
            if not ok:
                res["warn"].append(f"{name}{' ' + sub if sub else ''}: unknown flag {a}")
                res["help"][(path, name, sub)] = None
            if name == "sed" and a == "-i" and i + 1 < len(args) and re.match(r"-|.*/", args[i + 1]) and variant(path)[0] == "BSD":
                res["warn"].append("BSD sed: -i takes a backup suffix as its own argument, use: sed -i '' ...")
            i += 1 + (eat and name != "find")
            continue
        if name != "find":
            if sub or name in WRAPPERS or not re.fullmatch(r"[a-z][\w-]*", a) or not re.search(rf"(?m)^\s+{a}\b|\b{re.escape(name)}[ -]{a}\b", h):
                break  # first positional argument: options end here
            h2 = helptext(path, name, a)
            if len(h2.strip()) < 20:
                break
            sub, F = a, flagset(h + h2)
        i += 1


def check_cmd(toks, res, seen=()):
    s = res["s"]
    while toks and (toks[0] in KEYWORDS or ASSIGN.match(toks[0])):
        toks = toks[1:]
    if not toks or toks[0] in SKIPSEG or toks[0].startswith("\1") or "$" in toks[0]:
        return
    w, args = toks[0], toks[1:]
    kind, info = resolve(w, s)
    res["words"].add(w)
    if kind is None:
        if w not in res["defined"]:  # without a builtins list "missing" cannot be trusted
            (res["warn"] if "/" in w or not s["builtins"] else res["missing"]).append(f"command not found: {w}")
        return
    if kind == "alias" and w not in seen:
        return check_line(info + " " + " ".join(shlex.quote(a) for a in args), res, seen + (w,))
    name = os.path.basename(w)
    if kind == "file":
        check_flags(info, name, args, res)
        if name in ("sh", "bash", "zsh", "dash") and "-c" in args[:-1]:
            check_line(args[args.index("-c") + 1], res, seen)
    if name in WRAPPERS and not (name in ("command", "builtin") and args[:1] and args[0].startswith("-")):
        j = next((k for k, a in enumerate(args) if not a.startswith("-") and not ASSIGN.match(a) and resolve(a, s)[0]), None)
        if j is not None:
            check_cmd(args[j:], res, seen)


def close(text, k):
    depth, q = 0, ""
    for j in range(k, len(text)):
        c = text[j]
        if q:
            q = "" if c == q else q
        elif c in "'\"":
            q = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if not depth:
                return j
    return len(text)


def unsubst(line):
    """Replace $(..), `..`, <(..), $((..)) and ((..)) with placeholder words; return (line, inner command lines)."""
    out, inner, i, q = "", [], 0, ""
    while i < len(line):
        c = line[i]
        if q == "'":
            q = "" if c == "'" else q
        elif c == "\\":
            out += line[i:i + 2]
            i += 2
            continue
        elif c == '"':
            q = "" if q else '"'
        elif c == "'" and not q:
            q = "'"
        elif c == "`":
            j = line.find("`", i + 1) % (len(line) + 1) or len(line)
            inner.append(line[i + 1:j])
            out, i = out + "\1", j + 1
            continue
        elif line.startswith(("$(", "<(", ">("), i) or line.startswith("((", i) and not out.strip(" \t;&|("[-1:]):
            k = i + (line[i] != "(")
            j = close(line, k)
            if not line.startswith("((", k):
                inner.append(line[k + 1:j])
            out, i = out + ("\1" if line[i] != "(" else ":"), j + 1
            continue
        out += c
        i += 1
    return out, inner


def check_line(line, res, seen=()):
    line, inner = unsubst(line)
    for x in inner:
        check_line(x, res, seen)
    res["defined"].update(n for pair in re.findall(r"\bautoload\s+(?:-\w+\s+)*([\w-]+)|([\w-]+)\s*\(\)", line) for n in pair if n)
    lx = shlex.shlex(re.sub(r"\\;|';'|\";\"", " \0 ", line.replace("\n", ";")), posix=True, punctuation_chars=True)
    lx.whitespace_split, lx.commenters = True, ""
    try:
        toks = list(lx)
    except ValueError:
        return  # the shell's own -n check reports real syntax errors
    seg, i = [], 0
    while i <= len(toks):
        t = toks[i] if i < len(toks) else ";"
        if t and not t.strip("();<>|&"):
            if re.fullmatch(r"&?[<>]+[&|]?", t):
                if seg and seg[-1].isdigit():
                    seg.pop()
                i += 2
                continue
            check_cmd(seg, res, seen)
            seg = []
        else:
            seg.append(t)
        i += 1


def ps_parse(cmd, s):
    """(syntax error or None, the commands PowerShell itself sees, as [name, args...])"""
    try:
        p = subprocess.run([s["shell_path"]] + SHELLS["pwsh"][0] + [PS_PARSE], capture_output=True, encoding="utf-8", errors="replace",
                           timeout=10, stdin=subprocess.DEVNULL, env=dict(os.environ, PSST_CHECK=cmd))
    except (OSError, subprocess.TimeoutExpired):
        return None, []
    if p.returncode:
        return (p.stdout + p.stderr).strip() or "syntax error", []
    return None, [l.split("\t") for l in p.stdout.splitlines() if l.strip()]


def syntax(cmd, s):
    run_script, check, _ = SHELLS.get(s["shell"], SHELLS["bash"])
    if not check:
        return ps_parse(cmd, s)[0]
    argv = [s["shell_path"]] + check
    try:
        p = subprocess.run(argv, input=cmd, capture_output=True, text=True, timeout=10, env=dict(os.environ, PSST_CHECK=cmd))
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.returncode and ((p.stderr + p.stdout).strip() or "syntax error")


def validate(cmd, s, shell_check=True):
    res = {"s": s, "err": [], "warn": [], "missing": [], "words": set(), "defined": set(), "help": {}}
    if s["shell"] in ("pwsh", "powershell"):  # only PowerShell's parser tells commands from hashtable keys and .Members
        e, cmds = ps_parse(cmd, s)
        for toks in cmds:
            check_cmd(toks, res)
    else:
        e = shell_check and syntax(cmd, s)
        check_line(cmd, res)
    if e:
        res["err"].append(f"{s['shell']} -n: {e}")
    res["warn" if INSTALL.search(cmd) else "err"] += res["missing"]
    return res


def excerpt(path, name, sub):
    lines = helptext(path, name, sub).splitlines()
    keep = [i for i, l in enumerate(lines) if l.lstrip().startswith("-") or "usage" in l.lower()]
    return "\n".join(dict.fromkeys(lines[j] for i in keep for j in (i, i + 1) if j < len(lines)))[:4000]


# ---------- model ----------

def models():
    m = os.environ.get("PSST_MODEL")
    return [m] + [x for x in MODELS[1:] if x != m] if m else MODELS


def key_file():
    return Path(os.environ.get("XDG_CONFIG_HOME") or WIN and os.environ.get("APPDATA") or Path.home() / ".config") / "psst" / "key"


def api_key():
    """OPENROUTER_API_KEY, else the key saved by `psst --key`."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    try:
        return key or key_file().read_text().strip()
    except OSError:
        return ""


def set_key(key=""):
    """psst --key [KEY]: check the key with OpenRouter, then keep it in a file only you can read."""
    import getpass
    if not key:
        key = getpass.getpass("OpenRouter key (free at https://openrouter.ai/keys): ") if sys.stdin.isatty() else sys.stdin.readline()
    key, data = key.strip(), None
    if not key:
        print("psst: no key given", file=sys.stderr)
        return 1
    try:
        with urllib.request.urlopen(urllib.request.Request(API + "/key", headers={"Authorization": "Bearer " + key}), timeout=30) as r:
            data = json.load(r)["data"]
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print("psst: OpenRouter does not accept this key", file=sys.stderr)
            return 1
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        pass
    path = key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, key.encode() + b"\n")
    os.close(fd)
    os.chmod(path, 0o600)
    left = (data or {}).get("free_model_daily_requests", {}).get("remaining")
    print(f"psst: key saved to {path}" + (f"; {left} free requests left today" if left is not None else
                                           " (OpenRouter could not be reached to check it)" if data is None else ""), file=sys.stderr)
    return 0


def ask(messages):
    key = api_key()
    if not key:
        raise RuntimeError("no OpenRouter key: run `psst --key` (a free key is at https://openrouter.ai/keys)")
    time.sleep(max(0.0, STATS["last"] + STATS["gap"] - time.time()))
    body = {"models": models(), "messages": messages, "temperature": 0, "max_tokens": 4000, "provider": {"data_collection": "deny"}}
    req = urllib.request.Request(API + "/chat/completions", json.dumps(body).encode(),
                                 {"Authorization": "Bearer " + key, "Content-Type": "application/json", "X-Title": "psst"})
    STATS["requests"] += 1
    STATS["last"] = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())["error"]
            msg = err.get("metadata", {}).get("raw") or err.get("message")
        except (ValueError, KeyError, TypeError, AttributeError):
            msg = e.reason
        raise RuntimeError(f"OpenRouter HTTP {e.code}: {str(msg)[:200]}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"OpenRouter: {e}")
    if "error" in d or not d.get("choices"):
        raise RuntimeError(f"OpenRouter: {d.get('error', d)}")
    STATS["model"] = d.get("model", "")
    return d["choices"][0]["message"].get("content") or ""


def parse(text):
    text = re.sub(r"(?s)<think>.*?</think>", "", text or "")
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("command"), str) and obj["command"].strip():
            risk = str(obj.get("risk", "")).lower()
            return {"command": obj["command"].strip(), "explanation": " ".join(str(obj.get("explanation", "")).split()),
                    "risk": risk if risk in RISKS else "write", "utilities": obj.get("utilities") if isinstance(obj.get("utilities"), list) else []}
    raise ValueError("the reply has no JSON object with a non-empty \"command\"" if text.strip() else "empty reply")


def norm(task):
    return " ".join(task.lower().split()).strip(" ?.!")


def recall(task, s):
    hit = kv_get("ans\0" + s["shell"] + "\0" + norm(task))
    if hit and hit["fp"] == [stamp(SELF[-1])] + [fingerprint(w, s) for w in hit["val"]["words"]]:
        return dict(hit["val"], err=[], warn=[])
    return None


def remember(task, s, a):
    words = sorted(a["words"])
    kv_put("ans\0" + s["shell"] + "\0" + norm(task), [stamp(SELF[-1])] + [fingerprint(w, s) for w in words],
           {k: a[k] for k in ("command", "explanation", "risk")} | {"words": words})


def solve(task, s, use_cache=True):
    """Returns (final answer, first answer); each has command, explanation, risk, err, warn."""
    if not task and not s["last"]:
        raise RuntimeError("no previous command to fix")
    hit = use_cache and task and recall(task, s)
    if hit:
        return hit, hit
    ask_for = task or f"Fix my previous command {s['last']!r} (exit status {s['status'] or '?'}): it failed or did not do what I wanted."
    msgs = [{"role": "system", "content": SYSTEM.replace("{session}", snapshot(s)).replace("{examples}", examples(s))},
            {"role": "user", "content": ask_for}]
    tries = []
    for n in range(2):
        raw = ask(msgs)
        try:
            a = parse(raw)
            r = validate(a["command"], s)
            a.update(err=r["err"], warn=r["warn"], words=r["words"])
            problems = r["err"] + r["warn"]
            helps = "".join(f"\n\nHelp of `{name}{' ' + sub if sub else ''}` on this machine:\n{excerpt(path, name, sub)}" for path, name, sub in r["help"])
        except ValueError as e:
            a, problems, helps = None, [str(e)], ""
        tries.append(a)
        if not problems or n:
            break
        msgs += [{"role": "assistant", "content": raw or "(empty reply)"},
                 {"role": "user", "content": "Your command was checked on this machine and has problems:\n- " + "\n- ".join(problems) + helps
                  + "\n\nReply with the corrected JSON object only."}]
    good = [a for a in tries if a]
    if not good:
        raise RuntimeError("the model gave no usable answer: " + problems[0])
    final = good[-1]
    if use_cache and task and not final["err"] and not final["warn"]:
        remember(task, s, final)
    return final, tries[0]


# ---------- output ----------

def show(a):
    paint = (lambda code, t: f"\033[{code}m{t}\033[0m") if sys.stderr.isatty() else (lambda code, t: t)
    danger = a["risk"] in ("delete", "sudo") or DANGER.search(a["command"])
    print(paint("1;31" if danger else "1", a["command"]), file=sys.stderr)
    if a["explanation"]:
        print(paint("2", a["explanation"]), file=sys.stderr)
    for w in a["err"] + a["warn"]:
        print(paint("33", "! " + w), file=sys.stderr)


def inject(cmd):
    """Type the command into the terminal's input queue (TIOCSTI): it shows up at the next prompt, unexecuted."""
    if WIN or re.search(r"[\x00-\x1f\x7f]", cmd):
        return False
    import fcntl, termios
    try:
        with open("/dev/tty", "rb+", buffering=0) as tty:
            fd = tty.fileno()
            old = termios.tcgetattr(fd)
            quiet = termios.tcgetattr(fd)
            quiet[3] &= ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSANOW, quiet)
            try:
                for b in cmd.encode():
                    fcntl.ioctl(fd, termios.TIOCSTI, bytes([b]))
            finally:
                termios.tcsetattr(fd, termios.TCSANOW, old)
        return True
    except OSError:
        return False


def insert(cmd, s, integrated):
    """zsh and PowerShell integrations take stdout (print -z, history); otherwise type it in, else print it."""
    if integrated and s["shell"] in ("zsh", "pwsh", "powershell") or not inject(cmd) and (integrated or not sys.stdout.isatty()):
        print(cmd)


SELF = ([sys.executable] if getattr(sys, "frozen", False) else [os.path.abspath(__file__)] if not WIN and os.access(__file__, os.X_OK)
        else [sys.executable if WIN else "python3", os.path.abspath(__file__)])


def exe_cmd(sh, exe=None):
    exe = exe or SELF
    if sh in ("pwsh", "powershell"):
        return "& " + " ".join("'" + x.replace("'", "''") + "'" for x in exe)
    return " ".join(map(shlex.quote, exe))


def init(sh, exe=None):
    return INIT[sh].replace("PSST_EXE", exe_cmd(sh, exe))


INIT = {"zsh": r"""__psst() {
  local st=$? out
  out=$(PSST_SHELL=zsh PSST_SHELL_PATH="${commands[zsh]:-zsh}" PSST_SHELL_VERSION="$ZSH_VERSION" PSST_ALIASES="$(alias)" PSST_FUNCS="${(k)functions}" \
    PSST_LAST="$(fc -ln -2 2>/dev/null)" PSST_STATUS=$st PSST_EXE "$@") && print -rz -- "$out"
}
alias psst='noglob __psst'
""", "bash": r"""__psst() {
  local st=$__psst_st out
  out=$(PSST_SHELL=bash PSST_SHELL_PATH="$BASH" PSST_SHELL_VERSION="$BASH_VERSION" PSST_ALIASES="$(alias)" PSST_FUNCS="$(compgen -A function)" \
    PSST_LAST="$(fc -ln -2 2>/dev/null)" PSST_STATUS=$st PSST_EXE "$@")
  set +f
  [ -n "$out" ] && history -s "$out" && echo "psst: press Up to get the command" >&2
}
alias psst='__psst_st=$?; set -f; __psst'
""", "fish": r"""function psst
  set -l st $status
  set -l out (env PSST_SHELL=fish PSST_SHELL_PATH=(status fish-path 2>/dev/null; or command -s fish) PSST_SHELL_VERSION=$version \
    PSST_ALIASES=(alias | string collect) PSST_FUNCS=(functions -n | string join ' ') PSST_LAST=(history --max 2 --reverse | string collect) \
    PSST_STATUS=$st PSST_EXE $argv | string collect)
  test -n "$out"; and echo "psst: copy the command above" >&2
end
""", "pwsh": r"""function psst {
  $st = if ($?) { 0 } elseif ($LASTEXITCODE) { $LASTEXITCODE } else { 1 }
  $env:PSST_SHELL = if ($PSVersionTable.PSEdition -eq 'Core') { 'pwsh' } else { 'powershell' }
  $env:PSST_SHELL_PATH = (Get-Process -Id $PID).Path; $env:PSST_SHELL_VERSION = "$($PSVersionTable.PSVersion)"
  $env:PSST_ALIASES = (Get-Alias | ForEach-Object { "$($_.Name)=$($_.Definition)" }) -join "`n"
  $env:PSST_FUNCS = (Get-ChildItem Function: | ForEach-Object Name) -join ' '
  $env:PSST_LAST = (Get-History -Count 1).CommandLine; $env:PSST_STATUS = $st
  $enc = [Console]::OutputEncoding; [Console]::OutputEncoding = [Text.Encoding]::UTF8; $env:PYTHONIOENCODING = 'utf-8'
  $out = (PSST_EXE @args) -join "`n"
  [Console]::OutputEncoding = $enc
  Remove-Item Env:PYTHONIOENCODING, Env:PSST_SHELL, Env:PSST_SHELL_PATH, Env:PSST_SHELL_VERSION, Env:PSST_ALIASES, Env:PSST_FUNCS, Env:PSST_LAST, Env:PSST_STATUS -ErrorAction SilentlyContinue  # PSST_LAST is unset in a fresh session
  if ($out) { try { [Microsoft.PowerShell.PSConsoleReadLine]::AddToHistory($out); Write-Host 'psst: press Up to get the command' } catch {} }
}
"""}
INIT["powershell"] = INIT["pwsh"]


# ---------- shell integration: a marked block in the shell's rc file (fish: an autoloaded vendor_conf.d file) ----------

def rc_file(sh):
    home = Path.home()
    if sh == "zsh":
        return Path(os.environ.get("ZDOTDIR") or home) / ".zshrc"
    if sh == "bash":
        return home / (".bash_profile" if platform.system() == "Darwin" else ".bashrc")
    if sh == "fish":
        return Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share") / "fish/vendor_conf.d/psst.fish"
    if sh in ("pwsh", "powershell") and shutil.which(sh):
        return Path(run([shutil.which(sh)] + SHELLS[sh][0] + ["$PROFILE.CurrentUserAllHosts"], timeout=20).strip() or home / "psst-profile.ps1")
    return None


def block(sh):
    run_init = f"{exe_cmd(sh)} --init {sh}"
    if sh in ("pwsh", "powershell"):
        return f"{run_init} | Out-String | Invoke-Expression"
    return f"{run_init} | source" if sh == "fish" else f'eval "$({run_init})"'


def edit_rc(path, sh, add):
    """Add (or replace) or remove the marked block; returns True if the file changed."""
    old = path.read_text() if path.exists() else ""
    lines, out, skip = old.splitlines(keepends=True), [], False
    for line in lines:
        if line.strip() == BEGIN:
            skip = True
            if out and not out[-1].strip():
                out.pop()  # the blank line written before the block
        elif skip and line.strip() == END:
            skip = False
        elif not skip:
            out.append(line)
    text = "".join(out)
    if add:
        text += ("" if not text or text.endswith("\n") else "\n") + ("\n" if text.strip() else "") + f"{BEGIN}\n{block(sh)}\n{END}\n"
    if text == old:
        return False
    if not text.strip() and sh == "fish":
        path.unlink(missing_ok=True)
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return True


def setup(sh, yes=False):
    path = rc_file(sh)
    if not path:
        print(f"psst: no integration for {sh!r}; supported: zsh, bash, fish, pwsh", file=sys.stderr)
        return 1
    if sh != "fish" and not yes:  # fish loads vendor_conf.d by itself: no user config is edited
        reply = prompt(f"psst: add the {sh} integration to {path}? It puts commands into your input line and lets psst see "
                       f"aliases and the last command. [y/N] ")
        if reply not in ("y", "yes", "д", "да"):
            print(f"psst: not changed. To do it by hand, add to {path}:\n{block(sh)}", file=sys.stderr)
            return 0
    changed = edit_rc(path, sh, True)
    print(f"psst: {'updated' if changed else 'already set up in'} {path}; open a new terminal to use it", file=sys.stderr)
    return 0


def uninstall():
    for sh in ("zsh", "bash", "fish", "pwsh", "powershell"):
        path = rc_file(sh)
        if path and path.exists() and edit_rc(path, sh, False):
            print(f"psst: removed the integration from {path}", file=sys.stderr)
    for extra in (key_file(), db_dir()):
        shutil.rmtree(extra, ignore_errors=True) if extra.is_dir() else extra.unlink(missing_ok=True)
    print(f"psst: removed the integration, the saved key and the cache; the program itself is {SELF[-1]}", file=sys.stderr)
    return 0


def prompt(question):
    """Ask on the terminal itself: stdin and stdout may be pipes (installers, command substitution)."""
    try:
        with open("CONIN$" if WIN else "/dev/tty", "r") as tin:
            print(question, end="", file=sys.stderr, flush=True)
            return tin.readline().strip().lower()
    except OSError:
        return ""


def offer_setup(sh):
    """Without integration, ask once (per shell) whether to set it up."""
    path = sys.stdin.isatty() and not kv_get("asked\0" + sh) and rc_file(sh)
    if not path or path.exists() and BEGIN in path.read_text():  # set up already, just not loaded in this terminal
        return
    kv_put("asked\0" + sh, None, True)
    setup(sh)


# ---------- --selftest (offline; the model is mocked) ----------

CORRECT = r"""ls -la
ls -lhS --color=auto
grep -rn TODO .
grep -Eic 'err(or)?' log.txt
grep -rl --include='*.py' TODO .
grep -v '^#' app.conf | grep -c .
find . -type f -name '*.py' -mtime -7
find . -name '*.tmp' -print0 | xargs -0 rm -f
find . -type d -empty -delete
find . -name '*.sh' -exec chmod +x {} \;
find . -size +1M -exec ls -lh {} +
awk -F: '{print $1}' /etc/passwd
awk 'NR % 2 == 0' file.txt
sed -n '1,5p' file.txt
sed -e 's/a/b/' -e 's/c/d/' file.txt
sort -t, -k2,2nr scores.csv
sort names.txt | uniq -c | sort -rn | head -n 3
head -n 5 file.txt
tail -n 20 -f app.log
cut -d: -f1,3 /etc/passwd
cut -c1-5 file.txt
tr -s ' ' < file.txt
tr '[:lower:]' '[:upper:]' < a.txt > b.txt
wc -l *.py
du -sh * | sort -h
df -h .
tar -czf src.tgz --exclude='*.pyc' src
tar -xzf a.tgz -C out
cp -R src dst
mv -n a.txt b.txt
rm -rf build
mkdir -p a/b/c
ln -sf target link
chmod -R u+rwX dir
touch -t 202001020304 stamp
ps -ef | grep -v grep | grep python
ps -o pid,comm -p 1
kill -9 1234
xargs -n 1 echo < list.txt
xargs -I{} cp {} dst/ < list.txt
date +%Y-%m-%d
date -u '+%F %T'
seq 1 10 | paste -sd+ - | bc
split -l 100 big.txt part_
echo hi | base64
diff -u a.txt b.txt
git log --oneline -n 5
git commit -am 'msg'
git -C repo status -s
python3 -m venv .venv
python3 -c 'print(1)'
python3 -m pip install --user requests
curl -fsSL https://example.com -o page.html
ssh -p 2222 user@host
du -d 1 -h .
head -c 100 file.txt
comm -12 a.txt b.txt
jq -r '.name' x.json
sudo -u root ls -l
nice -n 10 tar -cf a.tar dir
env -i PATH=/bin ls -a
for f in *.jpeg; do mv -- "$f" "${f%.jpeg}.jpg"; done
echo "$(date +%s)" $((6*7))
(cd /tmp && ls -a)
[[ -f x ]] && cat x
if [ -d dir ]; then ls -1 dir; fi
ls 2>/dev/null | wc -l
sh -c 'ls -l | head -n 2'
find . -mmin -10 -print0 | xargs -0 ls -lh"""


def selftest():
    tmp = Path(tempfile.mkdtemp(prefix="psst-selftest-"))
    os.environ["XDG_CACHE_HOME"] = str(tmp / "cache")
    os.environ["XDG_CONFIG_HOME"] = str(tmp / "config")
    os.environ["OPENROUTER_API_KEY"] = "sk-selftest"  # never sent: answers come from the mock or the cache
    os.environ.pop("PSST_SHELL", None)
    global _db, ask
    _db, real_ask, fails, total = None, ask, [], [0]

    def ok(name, cond, detail=""):
        total[0] += 1
        if not cond:
            fails.append(f"{name} {detail}".rstrip())
    sh = os.path.basename(os.environ.get("SHELL", "")) if os.path.basename(os.environ.get("SHELL", "")) in ("zsh", "bash") else "zsh" if shutil.which("zsh") else "bash"
    env = dict(os.environ, PSST_SHELL=sh, PSST_SHELL_PATH=shutil.which(sh), PSST_ALIASES="ll='ls -lAh'", PSST_FUNCS="mkcd")
    s = session(env, cwd=str(tmp))

    # validator: correct commands for installed programs pass; the same commands broken three ways are caught
    forms = example_forms(s)
    good = [c for c in CORRECT.splitlines() + [c for _, c, _ in forms] if not validate(c, s, shell_check=False)["missing"]]
    good += ["ll -t", "mkcd build", "autoload -U zmv && zmv '(*).jpeg' '$1.jpg'"] * (sh == "zsh")
    for c in good:
        r = validate(c, s)
        ok("pass", not r["err"] and not r["warn"], f"{c!r}: {r['err'] + r['warn']}")
        w = c.split()[0]
        if resolve(w, s)[0] == "file":
            r = validate(c.replace(w, w + " --qzx-bogus", 1), s)
            ok("bad flag", any("qzx" in x for x in r["warn"]), repr(c))
            r = validate(c.replace(w, "qzxnope", 1), s)
            ok("missing", any("qzxnope" in x for x in r["err"] + r["warn"] * bool(INSTALL.search(c))), repr(c))
        ok("syntax", validate(c + " )", s)["err"], repr(c))
    ok("corpus size", len(good) >= 50, str(len(good)))
    ok("install makes missing a warning", not validate("brew install ripgrep && rg -n TODO", s)["err"])
    if shutil.which("pwsh"):  # PowerShell: hashtable keys and .Members are not commands (issue #1)
        ps = session(dict(os.environ, PSST_SHELL="pwsh", PSST_SHELL_PATH=shutil.which("pwsh")))
        for c in ["Get-ChildItem -File | ForEach-Object { [PSCustomObject]@{ Name = $_.Name; Lines = (Get-Content $_.FullName | Measure-Object -Line).Lines } }"
                  " | Sort-Object -Property Lines -Descending | Select-Object -First 5 Name, Lines",
                  "(Get-Item a.txt).Length", "$d = Get-Date; $d.AddDays(-7).ToString('yyyy-MM-dd')", "@{ a = 1 }.GetEnumerator() | Sort-Object Value",
                  "foreach ($f in Get-ChildItem *.txt) { (Get-Content $f).Count }"]:
            r = validate(c, ps)
            ok("pwsh: pass", not r["err"] and not r["warn"], f"{c!r}: {r['err'] + r['warn']}")
        ok("pwsh: missing", validate("Get-ChildItem | ForEach-Object { Get-Qzxnope -X }", ps)["err"])
        ok("pwsh: syntax", validate("Get-ChildItem | ForEach-Object {", ps)["err"])
    for _, _, rejected in forms:  # a variant-specific form for a missing tool or a BSD tool must be flagged
        for c in rejected:
            kind, info = resolve(c.split()[0], s)
            if kind != "file" or variant(info)[0] == "BSD":
                r = validate(c, s)
                ok("variant", r["err"] or r["warn"], repr(c))
    print(f"validator: {len(good)} correct commands; examples for this machine: " + "; ".join(c for _, c, _ in forms[:13]))

    # cache: add, overwrite and delete a binary in a PATH folder
    bindir = tmp / "bin"
    bindir.mkdir()
    tool = bindir / "qzt"
    benv = dict(env, PATH=f"{bindir}:{env['PATH']}")

    def put(text):
        time.sleep(0.01)
        tool.write_text(f"#!/bin/sh\necho '{text}'\n")
        tool.chmod(0o755)
    ok("cache: absent", resolve("qzt", session(benv))[0] is None)
    put("usage: qzt [-a]")
    s2 = session(benv)
    ok("cache: added", resolve("qzt", s2)[0] == "file")
    ok("cache: help v1", not validate("qzt -a", s2)["warn"] and validate("qzt -b", s2)["warn"])
    remember("run qzt", s2, {"command": "qzt -a", "explanation": "", "risk": "read", "words": {"qzt"}})
    ok("cache: answer hit", recall("run  QZT?", s2))
    put("usage: qzt [-b] [-c]")
    s2 = session(benv)
    ok("cache: help v2", not validate("qzt -b", s2)["warn"] and validate("qzt -a", s2)["warn"])
    ok("cache: answer stale", not recall("run qzt", s2))
    tool.unlink()
    ok("cache: deleted", validate("qzt", session(benv))["err"])

    # parser
    j = '{"command": "awk \'{print $1}\' f", "utilities": ["awk"], "explanation": "first column", "risk": "read"}'
    for text in ("```json\n" + j + "\n```", "Sure! " + j + " Hope it helps.", '<think>{"command": "bad"}</think>' + j):
        try:
            ok("parse", parse(text)["command"] == "awk '{print $1}' f", repr(text))
        except ValueError as e:
            ok("parse", False, f"{text!r}: {e}")
    for text in ("", "   ", '{"command": ls}', "no json here", '{"command": ""}'):
        try:
            parse(text)
            ok("parse rejects", False, repr(text))
        except ValueError:
            ok("parse rejects", True)

    # one repair with the exact error, no retries on errors
    calls = []
    replies = iter(['{"command": "ls --qzx-bogus", "risk": "read"}', '```json\n{"command": "ls -l", "risk": "read", "explanation": "list"}\n```'])
    ask = lambda m: calls.append(m) or next(replies)
    try:
        final, first = solve("list files", s, use_cache=False)
        ok("repair", len(calls) == 2 and first["warn"] and final["command"] == "ls -l" and not final["warn"] and "--qzx-bogus" in calls[1][-1]["content"]
           and "Help of `ls`" in calls[1][-1]["content"])
        calls, replies = [], iter(["", '{"command": "ls -l"}'])
        final, first = solve("list files", s, use_cache=False)
        ok("empty reply gets the one repair", first is None and final["command"] == "ls -l" and len(calls) == 2)
        ask = lambda m: (_ for _ in ()).throw(RuntimeError("HTTP 429"))
        try:
            solve("anything", s, use_cache=False)
            ok("no retry", False)
        except RuntimeError:
            ok("no retry", True)
    finally:
        ask = real_ask

    # --init: syntax and a real interactive shell receiving the command in its input line
    stub = tmp / "stub"  # stands in for python to show what the shell function passes
    stub.write_text('#!/bin/sh\nprintf "LAST=[%s] ST=[%s]\\n" "$PSST_LAST" "$PSST_STATUS" >&2\n')
    stub.chmod(0o755)
    legacy = Path("/proc/sys/dev/tty/legacy_tiocsti")
    typing = not legacy.exists() or legacy.read_text().strip() == "1"  # newer Linux kernels may forbid typing into a terminal
    for sh in [x for x in INIT if shutil.which(x)]:
        check = [shutil.which(sh)] + (SHELLS[sh][1] or SHELLS[sh][0] + [PS_PARSE])
        p = subprocess.run(check, input=init(sh), capture_output=True, text=True, env=dict(os.environ, PSST_CHECK=init(sh)))
        ok(f"init {sh} parses", p.returncode == 0, p.stderr + p.stdout)
        if sh not in ("zsh", "bash"):
            continue
        (tmp / f"init.{sh}").write_text(init(sh))
        (tmp / f"stub.{sh}").write_text(init(sh, [str(stub)]))
        remember("say hi", session(dict(env, PSST_SHELL=sh, PSST_SHELL_PATH=shutil.which(sh))), {"command": "echo QOK_$((6*7))", "explanation": "", "risk": "read", "words": set()})
        argv = [shutil.which(sh), "-f", "-i"] if sh == "zsh" else [shutil.which(sh), "--norc", "-i"]
        out = pty_session(argv, [f". {tmp}/init.{sh}\n", "psst say hi\n"], os.environ)
        ok(f"pty {sh}: not executed", "QOK_42" not in out.split("psst say hi", 1)[-1], repr(out[-300:]))
        out = pty_session(argv, [f". {tmp}/init.{sh}\n", "psst say hi\n", "\r"], os.environ)
        ok(f"pty {sh}: in input line", "QOK_42" in out.split("psst say hi", 1)[-1] if typing or sh == "zsh" else "press Up" in out, repr(out[-300:]))
        out = pty_session(argv, [f". {tmp}/stub.{sh}\n", "ls /qz-nonexistent\n", "psst\n"], os.environ)
        got = re.findall(r"LAST=\[(.*?)\] ST=\[(\d*)\]", out, re.S)
        ok(f"pty {sh}: last command", got and session({"PSST_LAST": got[-1][0], "PATH": ""})["last"] == "ls /qz-nonexistent" and got[-1][1] not in ("", "0"), repr(got))
        # simplified mode: no integration loaded; asks once about setup, then types the command in
        home = tmp / f"home-{sh}"
        home.mkdir()
        plain = dict(os.environ, HOME=str(home), ZDOTDIR=str(home))
        run_plain = f"{' '.join(map(shlex.quote, SELF))} say hi\n"
        out = pty_session(argv, [run_plain, "n\n", "\r"], plain)
        ok(f"plain {sh}: asks once", out.count("add the") == 1 and ("QOK_42" in out or not typing) and not list(home.iterdir()), repr(out[-400:]))
        out = pty_session(argv, [run_plain, "\r"], plain)
        ok(f"plain {sh}: does not ask again", "add the" not in out and ("QOK_42" in out or not typing), repr(out[-400:]))

    # key: saved privately, read back, the environment variable wins
    os.environ["OPENROUTER_API_KEY"] = ""
    global API
    real_api, API = API, "http://127.0.0.1:9"  # nothing listens there: the key is saved unchecked
    try:
        ok("key: saved", set_key("sk-test-1") == 0 and api_key() == "sk-test-1" and oct(key_file().stat().st_mode & 0o777) == "0o600")
        os.environ["OPENROUTER_API_KEY"] = "sk-env"
        ok("key: env wins", api_key() == "sk-env")
    finally:
        API = real_api
        os.environ["OPENROUTER_API_KEY"] = "sk-selftest"

    # rc block: added once, replaced not duplicated, removed cleanly; --setup -y and --uninstall end to end
    rc = tmp / "rc"
    rc.write_text("export A=1\n")
    edit_rc(rc, "zsh", True)
    edit_rc(rc, "zsh", True)
    text = rc.read_text()
    ok("rc: one block", text.count(BEGIN) == 1 and text.count(END) == 1 and text.startswith("export A=1\n\n" + BEGIN), repr(text))
    ok("rc: unchanged twice", not edit_rc(rc, "zsh", True))
    edit_rc(rc, "zsh", False)
    ok("rc: removed cleanly", rc.read_text() == "export A=1\n", repr(rc.read_text()))
    home = tmp / "home"
    home.mkdir()
    henv = dict(os.environ, HOME=str(home), ZDOTDIR=str(home), XDG_DATA_HOME=str(home / "share"))
    for sh in ("zsh", "fish"):
        subprocess.run(SELF + ["--setup", sh, "-y"], env=henv, capture_output=True)
    zrc, fishf = home / ".zshrc", home / "share/fish/vendor_conf.d/psst.fish"
    ok("setup: zsh block and fish file", zrc.is_file() and BEGIN in zrc.read_text() and fishf.is_file() and "--init fish | source" in fishf.read_text())
    subprocess.run(SELF + ["--uninstall"], env=henv, capture_output=True)
    ok("uninstall: block and fish file gone", BEGIN not in zrc.read_text() and not fishf.exists())

    shutil.rmtree(tmp, ignore_errors=True)
    for f in fails:
        print("FAIL", f)
    print(f"selftest: {total[0] - len(fails)}/{total[0]} passed")
    return 1 if fails else 0


def pty_session(argv, lines, env):
    import pty, select
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(argv[0], argv, dict(env, PS1="$ ", TERM="xterm", HISTFILE="/dev/null"))
    out = b""

    def read(quiet=0.6, longest=5):  # until the shell has been silent for a while
        nonlocal out
        end, last = time.time() + longest, time.time()
        while time.time() < end and time.time() - last < quiet:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    return
                last = time.time()
    read()
    for line in lines:
        os.write(fd, line.encode())
        read()
    read(quiet=2)
    os.kill(pid, 9)
    os.waitpid(pid, 0)
    return out.decode(errors="replace")


# ---------- --eval (live model; commands run only inside a temp dir) ----------

def rd(d, p):
    return (d / p).read_text() if (d / p).is_file() else None


def tree(d):
    return sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())


def utime(path, when):
    t = datetime.datetime(*when).timestamp() if isinstance(when, tuple) else when
    os.utime(path, (t, t))


def T(q, files=None, ok=None, run=True, setup=None, aliases=None, funcs=(), venv=False):
    return dict(q=q, files=files or {}, ok=ok, run=run, setup=setup, aliases=aliases or {}, funcs=funcs, venv=venv)


DAY = datetime.date.today()
TASKS = [
    T("в файлах *.conf замени localhost на 127.0.0.1 прямо в файлах", {"a.conf": "host=localhost\n", "b.conf": "db=localhost:5432\n", "keep.txt": "localhost\n"},
      lambda o, d: rd(d, "a.conf") == "host=127.0.0.1\n" and rd(d, "b.conf") == "db=127.0.0.1:5432\n" and tree(d) == ["a.conf", "b.conf", "keep.txt"]),
    T("delete every line containing DEBUG from app.log, editing the file in place", {"app.log": "INFO a\nDEBUG b\nINFO c\n"},
      lambda o, d: rd(d, "app.log") == "INFO a\nINFO c\n" and tree(d) == ["app.log"]),
    T("print the date 3 days ago as YYYY-MM-DD", ok=lambda o, d: o.strip() == str(DAY - datetime.timedelta(days=3))),
    T("какой день недели был 1 января 2000 года? выведи только название дня по-английски", ok=lambda o, d: o.strip() == "Saturday"),
    T("convert unix timestamp 1600000000 to UTC date and time formatted YYYY-MM-DD HH:MM:SS", ok=lambda o, d: o.strip() == "2020-09-13 12:26:40"),
    T("print only the size in bytes of data.bin", {"data.bin": "x" * 1234}, lambda o, d: o.strip() == "1234"),
    T("выведи время изменения файла old.txt в секундах unix", {"old.txt": "x"}, lambda o, d: o.strip() == "1500000000", setup=lambda d: utime(d / "old.txt", 1500000000)),
    T("show the permissions of run.sh as an octal number like 644", {"run.sh": "echo\n"}, lambda o, d: o.strip() in ("750", "0750"), setup=lambda d: (d / "run.sh").chmod(0o750)),
    T("list every .log file under logs/ with its size in bytes, one per line as: path size", {"logs/a.log": "x" * 10, "logs/sub/b.log": "abc", "logs/c.txt": "zz"},
      lambda o, d: {(Path(l.split()[0]).name, l.split()[-1]) for l in o.splitlines() if l.strip()} == {("a.log", "10"), ("b.log", "3")}),
    T("print the lines of list.txt in reverse order, last line first", {"list.txt": "1\n2\n3\n"}, lambda o, d: o == "3\n2\n1\n"),
    T("из ids.txt выведи только числа, которые идут после 'id='", {"ids.txt": "x id=12 y\nid=7\nnone\n"}, lambda o, d: o.split() == ["12", "7"]),
    T("squeeze every run of spaces into a single space in s.txt, in place", {"s.txt": "a   b  c\n"}, lambda o, d: rd(d, "s.txt") == "a b c\n" and tree(d) == ["s.txt"]),
    T("remove trailing whitespace from every line of code.py in place", {"code.py": "x = 1   \ny\t\nz\n"}, lambda o, d: rd(d, "code.py") == "x = 1\ny\nz\n" and tree(d) == ["code.py"]),
    T("how many CPU cores does this machine have? print just the number", ok=lambda o, d: o.strip() == str(os.cpu_count())),
    T("print only the MD5 hash of x.bin", {"x.bin": "hello\n"}, lambda o, d: o.strip() == hashlib.md5(b"hello\n").hexdigest()),
    T("посчитай sha256 файла x.bin и выведи только хеш", {"x.bin": "hello\n"}, lambda o, d: o.strip() == hashlib.sha256(b"hello\n").hexdigest()),
    T("print the current time in ISO 8601 with seconds and timezone offset", ok=lambda o, d: re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d([+-]\d\d:?\d\d|Z)", o.strip())),
    T("set the modification time of stamp.txt to 2021-05-06 07:08", {"stamp.txt": ""}, lambda o, d: (d / "stamp.txt").stat().st_mtime == datetime.datetime(2021, 5, 6, 7, 8).timestamp()),
    T("delete all .tmp files in this directory tree; file names may contain spaces", {"a b.tmp": "", "sub/c d.tmp": "", "keep.txt": ""}, lambda o, d: tree(d) == ["keep.txt"]),
    T("show the 2 biggest files in this directory tree with human-readable sizes, biggest first", {"big.bin": "x" * 300000, "sub/mid.bin": "x" * 200000, "small.txt": "x"},
      lambda o, d: "big.bin" in o and "mid.bin" in o and o.index("big.bin") < o.index("mid.bin") and "small.txt" not in o),
    T("create src.tar.gz from the folder src, excluding *.pyc files", {"src/a.py": "1", "src/b.pyc": "2", "src/sub/c.py": "3"},
      lambda o, d: (d / "src.tar.gz").is_file() and (lambda n: any(x.endswith("a.py") for x in n) and any(x.endswith("c.py") for x in n) and not any(x.endswith(".pyc") for x in n))(
          __import__("tarfile").open(d / "src.tar.gz").getnames())),
    T("split big.txt into files of 100 lines each whose names start with part_", {"big.txt": "".join(f"{i}\n" for i in range(250))},
      lambda o, d: len(list(d.glob("part_*"))) == 3 and "".join(p.read_text() for p in sorted(d.glob("part_*"))) == rd(d, "big.txt")),
    T("show the 3 most frequent words in words.txt with their counts", {"words.txt": "apple banana apple cherry banana apple date\n"},
      lambda o, d: "apple" in o and "banana" in o and o.index("apple") < o.index("banana") and "date" not in o),
    T("rename all .jpeg files in this folder to .jpg", {"a.jpeg": "1", "b c.jpeg": "2", "d.png": "3"}, lambda o, d: tree(d) == ["a.jpg", "b c.jpg", "d.png"]),
    T("list the names of .py files under this directory that contain TODO", {"a.py": "# TODO\n", "b.py": "x\n", "c.txt": "TODO\n", "sub/d.py": "TODO: x\n"},
      lambda o, d: {Path(l.strip()).name for l in o.splitlines() if l.strip()} == {"a.py", "d.py"}),
    T("найди файлы, изменённые за последние 10 минут", {"new.txt": "", "old.txt": ""}, lambda o, d: "new.txt" in o and "old.txt" not in o,
      setup=lambda d: utime(d / "old.txt", time.time() - 86400)),
    T("find files larger than 1 megabyte here", {"big.bin": "x" * 2_000_000, "small.bin": "x"}, lambda o, d: "big.bin" in o and "small.bin" not in o),
    T("decode the base64 string aGVsbG8gd29ybGQ=", ok=lambda o, d: o.strip() == "hello world"),
    T("convert win.txt from CRLF to LF line endings in place", {"win.txt": "a\r\nb\r\n"}, lambda o, d: (d / "win.txt").read_bytes() == b"a\nb\n" and tree(d) == ["win.txt"]),
    T("print the absolute fully resolved path of the symlink link", {"real/target.txt": "x"}, lambda o, d: o.strip() == os.path.realpath(d / "real/target.txt"),
      setup=lambda d: (d / "link").symlink_to("real/target.txt")),
    T("sort scores.csv by the second column numerically, highest first", {"scores.csv": "a,5\nb,12\nc,7\n"}, lambda o, d: o == "b,12\nc,7\na,5\n"),
    T("print the name of the most recently modified file here", {"new.txt": "", "old.txt": "", "mid.txt": ""}, lambda o, d: o.strip() == "new.txt",
      setup=lambda d: [utime(d / "old.txt", time.time() - 9000), utime(d / "mid.txt", time.time() - 5000)]),
    T("run ./slow.sh but kill it if it takes longer than 2 seconds", {"slow.sh": "touch started; sleep 5; touch done\n"},
      lambda o, d: (d / "started").exists() and not (d / "done").exists(), setup=lambda d: (d / "slow.sh").chmod(0o755)),
    T("создай структуру проекта моим алиасом", ok=lambda o, d: all((d / x).is_dir() for x in ("src", "tests", "docs")),
      aliases={"mkproj": "mkdir -p src tests docs", "ll": "ls -lAh"}),
    T("show a long listing of this folder including hidden files", {".hidden": "", "a.txt": ""}, lambda o, d: ".hidden" in o and "a.txt" in o,
      aliases={"ls": "ls -G", "ll": "ls -lAh"}),
    T("which python interpreter is active? print its full path", ok=lambda o, d: "/venv/bin/python" in o, venv=True),
    T("install the requests package into the active virtualenv", run=False, venv=True,
      ok=lambda c: re.search(r"\b(pip3?|python3? -m pip|uv pip) install\b.*\brequests\b", c) and not re.search(r"sudo|--user|brew|--break", c)),
    T("list packages installed in the current virtualenv", ok=lambda o, d: re.search(r"(?m)^pip\b", o), venv=True),
    T("deactivate the current virtual environment", run=False, venv=True, ok=lambda c: c.strip() == "deactivate"),
    T("run main.py with the active environment's python", {"main.py": "import sys; print(sys.prefix)\n"}, lambda o, d: o.strip().endswith("/venv"), venv=True),
]


EVAL_SH = shutil.which("zsh") or shutil.which("bash") or "sh"


def eval_env(t, root, d):
    env = {"PATH": os.environ["PATH"], "PSST_SHELL": os.path.basename(EVAL_SH), "PSST_SHELL_PATH": EVAL_SH,
           "PSST_SHELL_VERSION": (re.findall(r"\d+\.[\w.()-]+", run([EVAL_SH, "--version"])) or [""])[0],
           "PSST_ALIASES": "\n".join(f"{k}='{v}'" for k, v in t["aliases"].items()),
           "PSST_FUNCS": " ".join(list(t["funcs"]) + ["deactivate"] * t["venv"])}
    if t["venv"]:
        env.update(VIRTUAL_ENV=str(root / "venv"), PATH=f"{root / 'venv/bin'}:{env['PATH']}")
    return env


def fixture(t, root, n):
    d = root / n
    for p, text in t["files"].items():
        (d / p).parent.mkdir(parents=True, exist_ok=True)
        (d / p).write_text(text)
    d.mkdir(exist_ok=True)
    if t["setup"]:
        t["setup"](d)
    return d


def safe(cmd, root):
    """Run a generated command only if it touches nothing outside the eval folder and needs no network or root."""
    if re.search(r"\b(sudo|doas|curl|wget|ssh|scp|nc|brew|apt|npm|install|git\s+(push|pull|clone|fetch))\b", cmd):
        return False
    try:
        words = shlex.split(cmd)
    except ValueError:
        return False
    for part in (x for w in words for x in re.split(r"[=<>]+", w)):
        if part.startswith(("/", "~")) or ".." in part.split("/"):
            p = os.path.realpath(os.path.join(root, os.path.expanduser(part)))
            if p.startswith(root) or part.startswith("/dev/") or os.path.isfile(p) and os.access(p, os.X_OK):
                continue
            if os.path.exists(p) or os.path.isdir(os.path.dirname(p)):
                return False
    return True


def execute(cmd, t, root, n):
    if not t["run"]:
        return bool(t["ok"](cmd))
    d = fixture(t, root, n)
    if not safe(cmd, str(root)):
        return False
    script = root / f"{n}.sh"
    script.write_text("shopt -s expand_aliases\n" * ("bash" in EVAL_SH) + "".join(f"alias {k}={shlex.quote(v)}\n" for k, v in t["aliases"].items()) + "deactivate() { :; }\n" * t["venv"] + cmd + "\n")
    env = {"PATH": eval_env(t, root, d)["PATH"], "HOME": str(d), "TMPDIR": str(d), "LANG": "en_US.UTF-8", "TERM": "dumb"}
    if t["venv"]:
        env["VIRTUAL_ENV"] = str(root / "venv")
    try:
        p = subprocess.run([EVAL_SH] + ["-f"] * ("zsh" in EVAL_SH) + [str(script)], cwd=d, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        return bool(t["ok"](p.stdout, d))
    except Exception:
        return False


def evaluate(spec):
    picks = sorted({i for part in filter(None, spec.split(",")) for i in (range(int(part.split("-")[0]), int(part.split("-")[-1]) + 1))}) or range(1, len(TASKS) + 1)
    key = api_key()
    if not key:
        print("set OPENROUTER_API_KEY")
        return 1
    with urllib.request.urlopen(urllib.request.Request(API + "/key", headers={"Authorization": "Bearer " + key}), timeout=30) as r:
        left = json.load(r)["data"].get("free_model_daily_requests", {}).get("remaining")
    print(f"model: {models()[0]}; free requests left today: {left}")
    if left is not None and left < 2 * len(picks):
        print("not enough daily requests left for this run; stopping")
        return 1
    STATS["gap"] = 4.2  # stay under the per-model limit of 15 requests per minute
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="psst-eval-")))
    if any(TASKS[i - 1]["venv"] for i in picks):
        subprocess.run([sys.executable, "-m", "venv", str(root / "venv")], check=True)
    first_ok = final_ok = false_blocks = 0
    for i in picks:
        t = TASKS[i - 1]
        d = fixture(t, root, f"t{i}")
        try:
            final, first = solve(t["q"], session(eval_env(t, root, d), cwd=str(d)), use_cache=False)
        except RuntimeError as e:
            print(f"[{i:2}] ERROR {e}")
            if "HTTP 429" in str(e) and "per-day" in str(e).lower():
                break
            continue
        first = first or {"command": "(unusable reply)", "err": ["unusable reply"]}
        a = first["command"] != "(unusable reply)" and execute(first["command"], t, root, f"t{i}a")
        b = a if final is first else execute(final["command"], t, root, f"t{i}b")
        false_blocks += (a and bool(first["err"]) and final is first) + (b and bool(final["err"]))
        first_ok += a and not first["err"]
        final_ok += b and not final["err"]
        mark = lambda x, y: "✓" if x and not y["err"] else "✗"
        print(f"[{i:2}] {mark(a, first)}{mark(b, final)} {STATS['model'].split('/')[-1][:24]:24} {first['command']}"
              + ("" if final is first else f"\n{'':31}-> {final['command']}") + "".join(f"\n{'':31}! {w}" for w in final["err"] + final["warn"]))
    n = len(picks)
    print(f"\nfirst try: {first_ok}/{n} = {first_ok / n:.0%}; after repair: {final_ok}/{n} = {final_ok / n:.0%}; "
          f"false blocks: {false_blocks}; requests: {STATS['requests']}")
    shutil.rmtree(root, ignore_errors=True)
    return 0 if final_ok >= 0.9 * n and not false_blocks else 1


def main(argv):
    flag = argv[0] if argv and argv[0].startswith("--") else ""
    if flag == "--version":
        print(VERSION)
        return 0
    if flag == "--init" and argv[1:2] and argv[1] in INIT:
        print(init(argv[1]), end="")
        return 0
    if flag == "--setup":
        return setup(next((a for a in argv[1:] if a in INIT), detect_shell()), yes="-y" in argv or "--yes" in argv)
    if flag == "--key":
        return set_key(argv[1] if len(argv) > 1 else "")
    if flag == "--uninstall":
        return uninstall()
    if flag == "--selftest":
        return selftest()
    if flag == "--eval":
        return evaluate(argv[1] if len(argv) > 1 else "")
    if flag:
        print(__doc__.strip(), file=sys.stderr)
        return 0 if flag == "--help" else 2
    if not api_key() and sys.stdin.isatty() and set_key():  # first run: ask for the key once
        return 1
    integrated = "PSST_SHELL" in os.environ
    s = session()
    try:
        final, _ = solve(" ".join(argv).strip(), s)
    except RuntimeError as e:
        print(f"psst: {e}", file=sys.stderr)
        return 1
    show(final)
    if not integrated:
        offer_setup(s["shell"])  # before typing the command in, so the answer is not read from it
    if final["err"]:
        return 1
    insert(final["command"], s, integrated)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
