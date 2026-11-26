"""Paper identification helper for the Downloads sorter / paper-folder reorganisation.

Commands
  scan   <pdf>...                       -> JSON per file (DOI candidates, title guess)
  resolve <pdf> [--min-count N]         -> JSON Crossref-derived info for one PDF (or {} )
  plan-reorg <paper dir> <out.csv> [--main-dir D] [--trans-dir D] [--supp-dir D]
                                        -> reorganisation plan (no files moved)
  apply-reorg <plan.csv> <paper dir> <dup dir> <log.csv> [--unmatched-dir D]
  classify <file> <kind> <paper dir>   -> JSON {folder, name, note} for a new download (kind: main|supp|trans)
"""
import csv, json, os, re, sys, time, unicodedata, urllib.parse, urllib.request
from collections import Counter, defaultdict

import pymupdf

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "DownloadSorter")
os.makedirs(STATE, exist_ok=True)
CACHE_PATH = os.path.join(STATE, "crossref_cache.json")
INDEX_PATH = os.path.join(HERE, "paper_index.json")
UA = "DownloadSorter/1.0 (personal paper organiser; python-urllib)"
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
DOI_CTX_RE = re.compile(r"(?i)(?:doi[\s:=/\.>\"(]{0,6}|doi\.org/|dx\.doi\.org/)\s*(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)")
MAX_NAME = 80
# Existing-folder layout for plan-reorg / apply-reorg (override with --main-dir etc.)
LAYOUT = {"main": "main", "trans": "translations", "supp": "supplements", "unmatched": "_Unmatched"}
ACCEPT_TYPES = {"journal-article", "posted-content", "proceedings-article", "book-chapter", "review"}

# ---------------------------------------------------------------- utilities
def norm(s):
    s = unicodedata.normalize("NFKC", s).lower()
    return re.sub(r"[^0-9a-z]", "", s)

def clean_doi(d):
    d = re.sub(r"[\.\);,:/]+$", "", d)
    d = re.sub(r"(?i)(/-/dc\d*|/full|/abstract|/pdf|/epdf|\.pdf)$", "", d)
    if d.count("(") < d.count(")"):
        d = d[: d.rfind(")")]
    return d

def words(s):
    s = unicodedata.normalize("NFKC", s).lower()
    return {w for w in re.findall(r"[a-z0-9]+", s) if len(w) > 2}

def similarity(a, b):
    A, B = words(a), words(b)
    if not A or not B:
        return 0.0
    return len(A & B) / max(len(A), len(B))

def safe_name(s):
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(".")
    if len(s) > MAX_NAME:
        cut = s[:MAX_NAME]
        sp = cut.rfind(" ")
        if sp > MAX_NAME * 0.6:
            cut = cut[:sp]
        s = cut.rstrip(" -_,.;:")
    return s

# ---------------------------------------------------------------- crossref
_cache = None
def cache():
    global _cache
    if _cache is None:
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                _cache = json.load(f)
        except Exception:
            _cache = {}
    return _cache

def save_cache():
    if _cache is not None:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)

class NetError(Exception):
    pass

def http_json(url):
    c = cache()
    if url in c:
        return c[url]
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            data = None
        else:
            raise NetError(str(e))
    except Exception as e:
        raise NetError(str(e))
    c[url] = data
    time.sleep(0.05)
    return data

def work_by_doi(doi):
    d = http_json("https://api.crossref.org/works/" + urllib.parse.quote(doi, safe=""))
    return d["message"] if d else None

def work_by_pii(pii):
    d = http_json("https://api.crossref.org/works?rows=1&filter=alternative-id:" + pii)
    items = (d or {}).get("message", {}).get("items", [])
    return items[0] if items else None

def work_by_title(title):
    q = urllib.parse.quote(title[:250])
    d = http_json(f"https://api.crossref.org/works?rows=3&query.bibliographic={q}&select=DOI,title,author,issued,type,published-print,published-online,container-title,short-container-title")
    best, score = None, 0.0
    for it in (d or {}).get("message", {}).get("items", []):
        t = (it.get("title") or [""])[0]
        s = similarity(title, t)
        if s > score:
            best, score = it, s
    return (best, score) if best else (None, 0.0)

def describe(w):
    title = re.sub(r"<[^>]+>", "", (w.get("title") or [""])[0])
    title = re.sub(r"\s+", " ", title).strip()
    auth = w.get("author") or []
    first = next((a for a in auth if a.get("sequence") == "first"), auth[0] if auth else {})
    fam = first.get("family") or first.get("name") or "Unknown"
    if len(auth) >= 3:
        ap = f"{fam} et al."
    elif len(auth) == 2:
        ap = f"{fam} & {auth[1].get('family') or auth[1].get('name') or ''}".strip()
    else:
        ap = fam
    year = None
    for k in ("published-print", "published-online", "issued", "created"):
        dp = (w.get(k) or {}).get("date-parts")
        if dp and dp[0] and dp[0][0]:
            year = dp[0][0]
            break
    base = safe_name(f"{ap} {year or 'n.d.'} - {title}")
    return {"doi": w.get("DOI", "").lower(), "title": title, "author": ap, "year": year,
            "type": w.get("type"), "base": base}

# ---------------------------------------------------------------- PDF scan
def scan_pdf(path, pages=2):
    out = {"path": path, "dois": {}, "title_guess": "", "meta_title": "", "text_len": 0}
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        out["error"] = str(e)
        return out
    with doc:
        md = doc.metadata or {}
        out["meta_title"] = (md.get("title") or "").strip()
        blob = " ".join(str(v) for v in md.values() if v)
        try:
            xmp = doc.get_xml_metadata() or ""
        except Exception:
            xmp = ""
        texts = []
        for i in range(min(pages, doc.page_count)):
            pg = doc[i]
            texts.append(pg.get_text())
            for l in pg.get_links():
                if l.get("uri"):
                    texts.append(" " + l["uri"] + " ")
        text = "\n".join(texts)
        out["text_len"] = len(text.strip())
        cnt = Counter()
        for src in (blob, xmp, text):
            for m in DOI_CTX_RE.finditer(src):
                cnt[clean_doi(m.group(1)).lower()] += 1
        out["dois"] = dict(cnt.most_common(5))
        # title guess: largest-font text on page 1
        if doc.page_count:
            try:
                spans = []
                for b in doc[0].get_text("dict")["blocks"]:
                    for ln in b.get("lines", []):
                        for sp in ln.get("spans", []):
                            t = sp["text"].strip()
                            if t:
                                spans.append((round(sp["size"], 1), t))
                if spans:
                    sizes = sorted({s for s, t in spans if len(t) > 3}, reverse=True)
                    for sz in sizes[:3]:
                        cand = " ".join(t for s, t in spans if s == sz)
                        cand = re.sub(r"(?i)\b(author manuscript|hhs public access|europe pmc funders group|nih public access)\b", " ", cand)
                        cand = re.sub(r"\s+", " ", cand).strip()
                        if 20 <= len(cand) <= 300 and len(words(cand)) >= 4:
                            out["title_guess"] = cand
                            break
            except Exception:
                pass
    return out

# ---------------------------------------------------------------- filename keys
def strip_dup_suffix(stem):
    return re.sub(r"\s*\(\d+\)$", "", stem)

def filename_doi_guesses(name):
    b = strip_dup_suffix(os.path.splitext(name)[0])
    g = []
    m = re.match(r"^(s\d{5}-\d{3}-\d{4,6}-[\dxyz])", b, re.I)
    if m:
        g += [f"{p}/{m.group(1)}" for p in ("10.1038", "10.1007", "10.1186")]
    m = re.match(r"^(10\.\d{4,9})_(.+)$", b)
    if m:
        g.append(f"{m.group(1)}/{m.group(2)}")
    m = re.match(r"^(j\.\d{4}-\d{3}[\dX]\.\d{4}\.\d+\.x)$", b)
    if m:
        g.append("10.1111/" + m.group(1))
    m = re.match(r"^((ncomms|srep|nplants|nature)\d+)$", b)
    if m:
        g.append("10.1038/" + m.group(1))
    m = re.match(r"^(pnas\.\d+)$", b)
    if m:
        g.append("10.1073/" + m.group(1))
    m = re.match(r"^(science\.\d+)$", b)
    if m:
        g.append("10.1126/" + m.group(1))
    m = re.match(r"^(fpls|ijms|plants|foods|ijerph|agronomy|genes|cells)-(\d+)-(\d+)", b)
    if m and m.group(1) == "fpls":
        g.append(f"10.3389/fpls.20{int(m.group(2))+9:02d}.{m.group(3)}")  # fpls vol 4=2013
    return g

def pii_from_name(name):
    m = re.match(r"^1-s2\.0-(S?[\dX]{15,17})-main", name)
    return m.group(1) if m else None

def doi_keys(doi):
    """Article-id fragments a supplement/translation filename might carry."""
    suf = doi.split("/", 1)[1] if "/" in doi else doi
    keys = {norm(suf)}
    keys.add(norm(suf.split("/")[-1]))
    m = re.match(r"s(\d{5})-(\d{3})-0*(\d+)-", suf)
    if m:
        keys.add(f"springer:{m.group(1)}_{int(m.group(2))}_{int(m.group(3))}")
    return {k for k in keys if len(k) >= 6}

def supp_key(name):
    b = os.path.splitext(name)[0]
    m = re.match(r"^(\d{5})_(\d{4})_(\d+)_MOESM", b)
    if m:
        return f"springer:{m.group(1)}_{int(m.group(2)[1:])}_{int(m.group(3))}"
    m = re.match(r"^PP(\d{4})-[A-Z]+-(\d{5})", b)          # Plant Physiol manuscript no. -> pp.17.00794
    if m:
        return norm(f"pp.{m.group(1)[2:]}.{m.group(2)}")
    m = re.match(r"^(.+?)(?:[-_](?:suppl|sup-\d+|s\d+|supplementary|supplemental|supinfo|supmat|appendix)|\.sapp|_MOESM)", b, re.I)
    return norm(m.group(1)) if m else None

TRANS_PREFIX = re.compile(r"^(al-|ja-|translated_|t(?=[A-Z][a-z]+ to [A-Z]))", re.I)
def trans_stem(name):
    b = os.path.splitext(name)[0]
    if not TRANS_PREFIX.match(b) and not re.search(r"\s+ja$", b):
        return None
    b = TRANS_PREFIX.sub("", b)
    b = re.sub(r"\s+ja$", "", b)
    b = re.sub(r"(_\d)+$", "", strip_dup_suffix(b))
    b = re.sub(r"-[A-Z][a-z]+[A-Z]{3,}$", "", b)  # trailing "-GivenSURNAME" reader tag
    return norm(b)

def is_translation(name):
    return trans_stem(name) is not None or "翻訳" in name

# ---------------------------------------------------------------- resolve one main PDF
def resolve_main(path, min_count=1):
    name = os.path.basename(path)
    sc = scan_pdf(path)
    info, how = None, None
    for d, c in sc["dois"].items():
        if c < min_count:
            continue
        w = work_by_doi(d)
        if w:
            info, how = describe(w), f"pdf-doi x{c}"
            break
    if not info:
        for d in filename_doi_guesses(name):
            w = work_by_doi(d)
            if w:
                info, how = describe(w), "filename-doi"
                break
    if not info and pii_from_name(name):
        w = work_by_pii(pii_from_name(name))
        if w:
            info, how = describe(w), "elsevier-pii"
    if not info:
        for t in (sc["title_guess"], sc["meta_title"]):
            if t and len(words(t)) >= 4:
                w, s = work_by_title(t)
                if w and s >= 0.85:
                    info, how = describe(w), f"title-match {s:.2f}"
                    break
    if info:
        info["how"] = how
    return info, sc

# ---------------------------------------------------------------- index
def load_index():
    try:
        with open(INDEX_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"papers": []}

def save_index(idx):
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)

def index_entry(folder, info, orig_names):
    keys = set()
    for n in orig_names:
        keys.add(norm(strip_dup_suffix(os.path.splitext(n)[0])))
    doi = (info or {}).get("doi", "")
    if doi:
        keys |= doi_keys(doi)
    if info and info.get("title"):
        keys.add("title:" + norm(info["title"])[:60])
    return {"folder": folder, "doi": doi, "title": (info or {}).get("title", ""), "keys": sorted(k for k in keys if k)}

def find_paper(idx, name, sc=None, kind=None):
    """Return (entry, reason) for a supplement or translation file."""
    papers = idx["papers"]
    if kind == "trans" and trans_stem(name) is None:
        # file sits in the translation folder but has no translator prefix (e.g. ncomms3792.docx)
        stem = norm(re.sub(r"(_\d)+$", "", strip_dup_suffix(os.path.splitext(name)[0])))
        for p in papers:
            for k in p["keys"]:
                kk = k.split(":", 1)[-1]
                if len(stem) >= 6 and (stem == kk or (len(stem) >= 15 and kk.startswith(stem))):
                    return p, f"translation file name ~ {k}"
    # 1) DOI printed inside the file
    if sc:
        for d in sc.get("dois", {}):
            for p in papers:
                if p["doi"] and p["doi"] == d:
                    return p, f"doi {d}"
    # 2) translation: original filename / title
    ts = trans_stem(name)
    if ts:
        for p in papers:
            for k in p["keys"]:
                kk = k.split(":", 1)[-1]
                if len(kk) >= 6 and (ts == kk or (len(ts) >= 15 and (kk.startswith(ts) or ts.startswith(kk)))):
                    return p, f"translation name ~ {k}"
                # "Journal-Year-Author-Title..." style: title appears inside the stem
                if k.startswith("title:") and len(kk) >= 25 and kk[:40] in ts:
                    return p, f"translation name contains title"
    # 3) supplement article key
    sk = supp_key(name)
    if sk:
        for p in papers:
            for k in p["keys"]:
                if k == sk or (not sk.startswith("springer:") and len(sk) >= 6 and not k.startswith(("title:", "springer:")) and (sk in k)):
                    return p, f"supp key {sk}"
    # 4) title guess from file content
    if sc and sc.get("title_guess"):
        best, score = None, 0.0
        for p in papers:
            if p["title"]:
                s = similarity(sc["title_guess"], p["title"])
                if s > score:
                    best, score = p, s
        if best and score >= 0.8:
            return best, f"title {score:.2f}"
    return None, None

# ---------------------------------------------------------------- plan-reorg
def plan_reorg(root, out_csv):
    main_dir = os.path.join(root, LAYOUT["main"])
    trans_dir = os.path.join(root, LAYOUT["trans"])
    supp_dir = os.path.join(root, LAYOUT["supp"])
    rows = []
    papers_by_doi = {}
    idx = {"papers": []}
    used_folders = set()

    def unique_folder(base):
        f, i = base, 2
        while f.lower() in used_folders:
            f, i = f"{base} ({i})", i + 1
        used_folders.add(f.lower())
        return f

    mains = sorted(os.listdir(main_dir))
    for i, n in enumerate(mains, 1):
        p = os.path.join(main_dir, n)
        if not os.path.isfile(p):
            continue
        print(f"[main {i}/{len(mains)}] {n}", file=sys.stderr)
        info, sc = resolve_main(p) if n.lower().endswith(".pdf") else (None, {})
        if info and info["doi"] in papers_by_doi:
            entry = papers_by_doi[info["doi"]]
            rows.append({"kind": "main-dup", "src": p, "folder": entry["folder"], "name": n, "how": info["how"], "doi": info["doi"], "title": info["title"]})
            entry["orig"].append(n)
            continue
        if info:
            folder = unique_folder(info["base"])
            name = folder + ".pdf"
        else:
            folder = unique_folder(safe_name(strip_dup_suffix(os.path.splitext(n)[0])))
            name = n
        entry = {"folder": folder, "info": info, "orig": [n]}
        if info:
            papers_by_doi[info["doi"]] = entry
        rows.append({"kind": "main", "src": p, "folder": folder, "name": name, "how": (info or {}).get("how", "unresolved"), "doi": (info or {}).get("doi", ""), "title": (info or {}).get("title", "")})
        idx["papers"].append(entry)

    idx = {"papers": [index_entry(e["folder"], e["info"], e["orig"]) for e in idx["papers"]]}

    for kind, d in (("trans", trans_dir), ("supp", supp_dir)):
        for n in sorted(os.listdir(d)):
            p = os.path.join(d, n)
            if not os.path.isfile(p):
                continue
            print(f"[{kind}] {n}", file=sys.stderr)
            sc = scan_pdf(p) if n.lower().endswith(".pdf") else None
            entry, why = find_paper(idx, n, sc, kind)
            if entry:
                if kind == "trans":
                    name = entry["folder"] + "_翻訳" + os.path.splitext(n)[1].lower()
                else:
                    name = n
                rows.append({"kind": kind, "src": p, "folder": entry["folder"], "name": name, "how": why, "doi": entry["doi"], "title": entry["title"]})
            else:
                rows.append({"kind": kind + "-unmatched", "src": p, "folder": "", "name": n, "how": "", "doi": "", "title": (sc or {}).get("title_guess", "")})
    save_cache()
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["kind", "src", "folder", "name", "how", "doi", "title"])
        w.writeheader()
        w.writerows(rows)
    with open(out_csv + ".index.json", "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    c = Counter(r["kind"] for r in rows)
    print(json.dumps(c, ensure_ascii=False))

# ---------------------------------------------------------------- apply-reorg
def file_hash(p):
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

def apply_reorg(plan_csv, root, dup_dir, log_csv):
    import shutil
    from datetime import datetime
    with open(plan_csv, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    with open(plan_csv + ".index.json", encoding="utf-8") as f:
        idx = json.load(f)
    unmatched_dir = {"trans-unmatched": os.path.join(root, LAYOUT["unmatched"], "translations"),
                     "supp-unmatched": os.path.join(root, LAYOUT["unmatched"], "supplements")}
    log = []
    hashes = {}

    def h(p):
        if p not in hashes:
            hashes[p] = file_hash(p)
        return hashes[p]

    def free(path):
        b, e = os.path.splitext(path)
        i = 2
        while os.path.exists(path):
            path = f"{b} ({i}){e}"
            i += 1
        return path

    def move(src, dst, rule, note=""):
        if not os.path.exists(src):
            log.append([datetime.now().isoformat(timespec="seconds"), src, "", rule, "ERROR: source missing"])
            return
        if os.path.exists(dst):
            if h(dst) == h(src):
                dst = free(os.path.join(dup_dir, os.path.basename(src)))
                note = (note + " duplicate").strip()
            else:
                dst = free(dst)
                note = (note + " numbered").strip()
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.move(src, dst)
            hashes[dst] = hashes.pop(src, None) or hashes.get(dst)
            log.append([datetime.now().isoformat(timespec="seconds"), src, dst, rule, note])
        except Exception as e:
            log.append([datetime.now().isoformat(timespec="seconds"), src, dst, rule, f"ERROR: {e}"])

    order = {"main": 0, "main-dup": 1, "trans": 2, "supp": 3, "trans-unmatched": 4, "supp-unmatched": 5}
    for r in sorted(rows, key=lambda r: order[r["kind"]]):
        k = r["kind"]
        if k in unmatched_dir:
            move(r["src"], os.path.join(unmatched_dir[k], r["name"]), "paper-reorg:" + k)
            continue
        folder = os.path.join(root, r["folder"])
        if k == "main-dup":
            move(r["src"], os.path.join(folder, r["name"]), "paper-reorg:main-dup", r["how"])
        else:
            move(r["src"], os.path.join(folder, r["name"]), "paper-reorg:" + k, f"{r['how']} doi={r['doi']}")
    new = not os.path.exists(log_csv)
    with open(log_csv, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        if new:
            w.writerow(["Time", "From", "To", "Rule", "Note"])
        w.writerows(log)
    save_index(idx)
    for d in (LAYOUT["main"], LAYOUT["trans"], LAYOUT["supp"]):
        p = os.path.join(root, d)
        try:
            if os.path.isdir(p) and not os.listdir(p):
                os.rmdir(p)
        except OSError as e:
            print(f"note: could not remove empty folder {p}: {e}", file=sys.stderr)
    errs = [l for l in log if l[4].startswith("ERROR")]
    print(json.dumps({"moved": len(log) - len(errs), "errors": len(errs),
                      "duplicates": sum(1 for l in log if "duplicate" in l[4]),
                      "numbered": sum(1 for l in log if "numbered" in l[4])}, ensure_ascii=False))
    for e in errs:
        print("ERR", e[1], e[4])

# ---------------------------------------------------------------- classify (new downloads)
def classify(path, kind, root, min_count=1):
    name = os.path.basename(path)
    idx = load_index()
    try:
        if kind == "main":
            info, sc = resolve_main(path, min_count=min_count)
            if not info:
                return {"status": "unresolved"}
            for p in idx["papers"]:
                if p["doi"] == info["doi"]:
                    return {"status": "ok", "folder": p["folder"], "name": p["folder"] + ".pdf", "note": f"existing paper ({info['how']})", "doi": info["doi"], "type": info["type"]}
            folder, i = info["base"], 2
            existing = {p["folder"].lower() for p in idx["papers"]}
            while folder.lower() in existing or os.path.exists(os.path.join(root, folder)):
                folder, i = f"{info['base']} ({i})", i + 1
            idx["papers"].append(index_entry(folder, info, [name]))
            save_index(idx)
            return {"status": "ok", "folder": folder, "name": folder + ".pdf", "note": f"new paper ({info['how']})", "doi": info["doi"], "type": info["type"]}
        sc = scan_pdf(path) if name.lower().endswith(".pdf") else None
        entry, why = find_paper(idx, name, sc, kind)
        if not entry:
            return {"status": "unmatched"}
        nm = entry["folder"] + "_翻訳" + os.path.splitext(name)[1].lower() if kind == "trans" else name
        return {"status": "ok", "folder": entry["folder"], "name": nm, "note": why, "doi": entry["doi"]}
    except NetError as e:
        return {"status": "neterror", "note": str(e)}
    finally:
        save_cache()

# ---------------------------------------------------------------- main
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    for flag, key in (("--main-dir", "main"), ("--trans-dir", "trans"), ("--supp-dir", "supp"), ("--unmatched-dir", "unmatched")):
        if flag in args:
            i = args.index(flag)
            LAYOUT[key] = args[i + 1]
            del args[i:i + 2]
    sys.argv = [sys.argv[0]] + args
    cmd = sys.argv[1]
    if cmd == "scan":
        for p in sys.argv[2:]:
            print(json.dumps(scan_pdf(p), ensure_ascii=False))
    elif cmd == "plan-reorg":
        plan_reorg(sys.argv[2], sys.argv[3])
    elif cmd == "apply-reorg":
        apply_reorg(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    elif cmd == "classify":
        mc = int(sys.argv[5]) if len(sys.argv) > 5 else 1
        if len(sys.argv) > 6 and sys.argv[6] == "dry":
            save_index = lambda idx: None  # noqa: E731  (dry run: don't register new papers)
        print(json.dumps(classify(sys.argv[2], sys.argv[3], sys.argv[4], mc), ensure_ascii=False))
