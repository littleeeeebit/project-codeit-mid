"""Generate a live source notebook and compare a fixed commit with the working files."""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import html
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NOTEBOOK = "notebooks/before_after.ipynb"
INPUT = "notebooks/cases.json"
BASELINE = "notebooks/baseline.json"
SOURCE_PATHS = ("src", "pyproject.toml", "requirements.txt", "requirements-lock.txt")
STAGES = {
    "data": ("데이터", "원문을 읽고 검색할 조각으로 나눕니다. 표의 조건이나 숫자가 사라졌는지 봅니다.",
             {"corpus/ingestion.py": ["walk_hwp", "finalize_elements"],
              "retrieval/chunking.py": ["build_chunks"]}),
    "bm25": ("BM25", "질문을 검색어로 바꿉니다. 실제 검색 결과는 아래 리트리버에서 함께 확인합니다.",
             {"retrieval/retrieval.py": ["normalize_for_analysis", "Analyzer", "rank_lexical"]}),
    "dense": ("덴스", "벡터 정규화와 잘못된 입력 거부를 비교합니다. 임베딩 모델의 검색 품질은 여기서 측정하지 않습니다.",
              {"retrieval/dense.py": ["unit_vector", "embed_policy"]}),
    "models": ("모델 구성", "등록된 모델과 옵션의 변경을 보여줍니다. 구성 변경만으로 성능 향상을 판정하지 않습니다.",
               {"retrieval/models.py": ["EmbeddingSpec", "EMBEDDINGS", "RERANKERS"]}),
    "parameters": ("파라미터", "코드에 정의된 기본값을 비교합니다. 운영 서버의 활성 설정은 별도입니다.",
                   {"settings.py": ["Settings"]}),
    "retriever": ("리트리버", "같은 질문과 문서로 검색합니다. 순위, 찾은 문장, 제외된 문장과 이유를 나란히 봅니다.",
                  {"retrieval/retrieval.py": ["retrieve", "fuse", "_pack_evidence"]}),
    "context": ("컨텍스트 엔지니어링", "검색한 근거로 실제 프롬프트를 만듭니다. 들어간 문장과 최종 토큰 수를 비교합니다.",
                {"gateway/generation.py": ["SYSTEM_PROMPT", "build_messages", "count_request_tokens"]}),
    "generation": ("GENERATION", "저장된 답변의 인용 검사를 다시 실행합니다. 새 LLM 답변의 품질이나 API 비용은 미측정입니다.",
                   {"gateway/generation.py": ["validate_answer", "status_problem"]}),
    "logging": ("기록", "로그에 남기기 전 비밀값을 가리는 동작을 비교합니다. 외부 로그 서버 전송은 실행하지 않습니다.",
                {"gateway/tracing.py": ["redact", "mask"]}),
}


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def digest(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for name, data in sorted(files.items()):
        h.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()


def write_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(value, encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


def current_files(root: Path) -> dict[str, bytes]:
    names = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *SOURCE_PATHS)
    files = {}
    for name in names.decode("utf-8").split("\0"):
        if not name:
            continue
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"source path leaves the repository: {name}")
        if path.is_file():
            files[name] = path.read_bytes()
    return files


def baseline_files(root: Path) -> tuple[str, dict[str, bytes]]:
    ref = json.loads((root / BASELINE).read_text(encoding="utf-8"))["commit"]
    if len(ref) != 40 or any(c not in "0123456789abcdef" for c in ref):
        raise ValueError("baseline must be a full commit hash; use the baseline command")
    archive = git(root, "archive", "--format=zip", ref, "--", *SOURCE_PATHS)
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        files = {p.filename: z.read(p) for p in z.infolist() if not p.is_dir()}
    return ref, files


def source_excerpt(source: str, names: list[str]) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"Source has a syntax error: {exc}\n\n{source}"
    lines = source.splitlines()
    excerpts = []
    for node in tree.body:
        targets = [getattr(node, "name", None)]
        if isinstance(node, ast.Assign):
            targets += [getattr(t, "id", None) for t in node.targets]
        if isinstance(node, ast.AnnAssign):
            targets.append(getattr(node.target, "id", None))
        if set(targets) & set(names):
            start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            excerpts.append("\n".join(lines[start - 1:node.end_lineno]))
    return "\n\n".join(excerpts) or "The documented entry point is no longer present; update its stage mapping."


def cell(kind: str, text: str, key: str) -> dict:
    result = {"cell_type": kind, "id": key, "metadata": {}, "source": text.splitlines(keepends=True)}
    if kind == "code":
        result.update(execution_count=None, outputs=[])
    return result


def sync(root: Path = ROOT) -> bool:
    files = current_files(root)
    watched = {**files, **{name: (root / name).read_bytes() for name in
                          (BASELINE, INPUT, "tools/notebooks/compare.py", "tools/notebooks/replay.py")}}
    identity = digest(watched)
    destination = root / NOTEBOOK
    if destination.exists():
        old = json.loads(destination.read_text(encoding="utf-8"))
        if old.get("metadata", {}).get("bidmate_source") == identity:
            return False
    ref = json.loads(watched[BASELINE])["commit"]
    cells = [cell("markdown", "# 수정 전 · 수정 후\n\n"
        "`.py`를 수정하고 저장한 뒤 **Run All**을 누르세요. 아래 원본 코드는 저장할 때 자동 갱신됩니다.\n\n"
        f"A는 고정 커밋 `{ref}`이고, B는 실행 시점의 작업 파일입니다. 커밋하지 않은 수정도 포함됩니다. "
        "같은 입력과 같은 검사로 두 버전을 별도 프로세스에서 실행합니다.\n\n"
        "입력은 `cases.json`의 작은 회귀 검사 묶음입니다. 전체 서비스 품질 점수가 아닙니다. "
        "API 호출·DB 접속 없이 실행하며, 새 답변 생성과 임베딩 모델 품질은 미측정으로 남깁니다.\n\n"
        "소스 갱신 시 이전 출력은 지웁니다. 이미 열린 화면은 파일을 다시 열어야 갱신될 수 있습니다. "
        "기준 A는 자동으로 바뀌지 않습니다.", "intro"),
        cell("code", "from pathlib import Path\nimport importlib\nimport sys\n"
             "from IPython.display import HTML, display\n\n"
             "ROOT = next(p for p in (Path.cwd(), *Path.cwd().parents)\n"
             "            if (p / 'tools/notebooks/compare.py').is_file())\n"
             "if str(ROOT) not in sys.path:\n    sys.path.insert(0, str(ROOT))\n"
             "from tools.notebooks import compare\ncompare = importlib.reload(compare)\n"
             "compare.start_watcher(ROOT)\n"
             "report = compare.run_comparison(ROOT)\n"
             "display(HTML(compare.render_report(report)))\n", "run")]
    for key, (title, description, sources) in STAGES.items():
        text = f"## {title}\n\n{description}\n\n원본 함수는 아래에서 펼쳐 볼 수 있습니다. 실행은 위에서 복사한 코드가 아닌 `.py` 스냅샷을 사용합니다.\n"
        for path, names in sources.items():
            name = "src/rfp_assistant/" + path
            content = files.get(name, b"").decode("utf-8")
            text += (f"\n<details><summary>{html.escape(name)}</summary>\n"
                     f"<pre>{html.escape(source_excerpt(content, names))}</pre>\n</details>\n")
        cells += [cell("markdown", text, "source-" + key),
                  cell("code", f"display(HTML(compare.render_stage(report, {key!r})))\n", "result-" + key)]
    notebook = {"cells": cells, "metadata": {
        "kernelspec": {"display_name": "Python (rfp-assistant)", "language": "python", "name": "rfp-assistant"},
        "language_info": {"name": "python", "version": "3.12"}, "bidmate_source": identity},
        "nbformat": 4, "nbformat_minor": 5}
    write_atomic(destination, encoded(notebook) + "\n")
    return True


def watch(root: Path = ROOT, stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    last_error = None
    while not stop.is_set():
        try:
            if sync(root):
                print("Notebook source refreshed; previous outputs cleared.", flush=True)
            last_error = None
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            if str(exc) != last_error:
                print(f"Notebook refresh failed: {exc}", file=sys.stderr, flush=True)
                last_error = str(exc)
        stop.wait(1)


def start_watcher(root: Path = ROOT) -> None:
    # Store the handle on threading so reloading this module does not duplicate its watcher.
    key = "_bidmate_notebook_" + hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]
    previous = getattr(threading, key, None)
    if previous and previous[0].is_alive():
        previous[1].set()
        previous[0].join(timeout=5)
        if previous[0].is_alive():
            raise RuntimeError("previous notebook watcher is still stopping")
    stop = threading.Event()
    worker = threading.Thread(target=watch, args=(root, stop), daemon=True)
    setattr(threading, key, (worker, stop))
    worker.start()


def materialize(files: dict[str, bytes], destination: Path) -> None:
    for name, content in files.items():
        path = destination / name
        if not path.resolve().is_relative_to(destination.resolve()):
            raise ValueError(f"snapshot path leaves its destination: {name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def execute(snapshot: Path, inputs: Path, harness: Path, output: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(
        ("RFP_", "BIDMATE_", "OPENAI_", "LANGFUSE_", "GEMINI_", "GOOGLE_API_"))}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        completed = subprocess.run([sys.executable, "-I", "-B", str(harness), str(snapshot), str(inputs), str(output)],
                                   cwd=snapshot, env=env, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {"observations": [], "error": "Replay exceeded 180 seconds"}
    if completed.returncode or not output.exists():
        return {"observations": [], "error": completed.stderr.decode("utf-8", errors="replace")[-6000:]}
    return json.loads(output.read_text(encoding="utf-8"))


def compare_rows(before: dict, after: dict) -> list[dict]:
    a = {row["id"]: row for row in before["observations"]}
    b = {row["id"]: row for row in after["observations"]}
    rows = []
    for key in dict.fromkeys([*a, *b]):
        left, right = a.get(key), b.get(key)
        if left is None or right is None:
            status = "비교 불가"
        else:
            passed_a = all(left["checks"].values()) and not left.get("error")
            passed_b = all(right["checks"].values()) and not right.get("error")
            if passed_a and not passed_b:
                status = "악화"
            elif not passed_a and passed_b:
                status = "개선"
            elif not passed_a:
                status = "양쪽 실패"
            elif left["output"] != right["output"] or left["checks"] != right["checks"]:
                status = "변경됨"
            else:
                status = "동일"
        rows.append({"id": key, "stage": (right or left)["stage"], "status": status, "before": left, "after": right})
    return rows


def run_comparison(root: Path = ROOT, input_path: Path | None = None) -> dict:
    root = root.resolve()
    ref, before_files = baseline_files(root)
    after_files = current_files(root)
    input_bytes = (input_path or root / INPUT).read_bytes()
    bundle = json.loads(input_bytes)
    validate_bundle(bundle)
    harness = (root / "tools/notebooks/replay.py").read_bytes()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    destination = root / ".runtime" / "notebook-comparisons" / run_id
    destination.mkdir(parents=True)
    (destination / "cases.json").write_bytes(input_bytes)
    (destination / "replay.py").write_bytes(harness)
    versions = sorted((d.metadata["Name"], d.version) for d in metadata.distributions() if d.metadata["Name"])
    report = {"run_id": run_id, "baseline_commit": ref, "before_sha": digest(before_files),
              "after_sha": digest(after_files), "input_sha": hashlib.sha256(input_bytes).hexdigest(),
              "harness_sha": hashlib.sha256(harness).hexdigest(), "python": sys.version,
              "packages": versions, "input_description": bundle["description"], "directory": str(destination),
              "dependency_files_changed": [p for p in SOURCE_PATHS[1:] if before_files.get(p) != after_files.get(p)]}
    # Preserve the exact dirty working files, not a Git diff that would lose untracked additions.
    for label, files in (("before", before_files), ("after", after_files)):
        with zipfile.ZipFile(destination / f"{label}.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(files.items()):
                archive.writestr(name, content)
        with tempfile.TemporaryDirectory(prefix="bidmate-notebook-") as tmp:
            snapshot = Path(tmp)
            materialize(files, snapshot)
            report[label] = execute(snapshot, destination / "cases.json", destination / "replay.py",
                                    destination / f"{label}.json")
    report["rows"] = compare_rows(report["before"], report["after"])
    report["source_diff"] = "\n".join("\n".join(difflib.unified_diff(
        before_files.get(name, b"").decode("utf-8", errors="replace").splitlines(),
        after_files.get(name, b"").decode("utf-8", errors="replace").splitlines(),
        fromfile="A/" + name, tofile="B/" + name, lineterm=""))
        for name in sorted(before_files.keys() | after_files.keys()) if before_files.get(name) != after_files.get(name))
    report["working_files_changed_during_run"] = digest(current_files(root)) != report["after_sha"]
    write_atomic(destination / "report.json", encoded(report) + "\n")
    write_atomic(destination / "report.html", "<!doctype html><html lang='ko'><meta charset='utf-8'>"
                 "<title>수정 전 · 수정 후</title><body>" + render_report(report)
                 + "".join(render_stage(report, key) for key in STAGES) + "</body></html>")
    return report


def validate_bundle(bundle: dict) -> None:
    required = ("description", "as_of", "documents", "questions", "fusion", "vectors", "answers", "logging")
    if not isinstance(bundle, dict) or any(key not in bundle for key in required):
        raise ValueError(f"input bundle requires {required}")
    for key in required[2:]:
        rows = bundle[key]
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get("id"), str) for r in rows):
            raise ValueError(f"{key} must contain objects with string IDs")
        if len({r["id"] for r in rows}) != len(rows):
            raise ValueError(f"duplicate IDs in {key}")
    if not bundle["documents"] or not bundle["questions"]:
        raise ValueError("at least one document and one question are required")
    docs = {d["id"] for d in bundle["documents"]}
    for doc in bundle["documents"]:
        if (not isinstance(doc.get("xml"), str) or not isinstance(doc.get("source_hash"), str)
                or not isinstance(doc.get("must_preserve"), list) or not doc["must_preserve"]
                or any(not isinstance(p, str) or not p for p in doc["must_preserve"])):
            raise ValueError(f"document {doc['id']} needs XML, source_hash and preservation phrases")
    for q in bundle["questions"]:
        if (not isinstance(q.get("question"), str) or not q["question"].strip()
                or not isinstance(q.get("documents"), list) or not q["documents"]
                or any(not isinstance(d, str) or d not in docs for d in q["documents"])
                or not isinstance(q.get("required"), list)
                or any(not isinstance(p, str) or not p for p in q["required"])):
            raise ValueError(f"question {q['id']} needs known documents and a required-evidence list")
        if "expect_empty" in q and not isinstance(q["expect_empty"], bool):
            raise ValueError(f"question {q['id']} expect_empty must be boolean")
        if not q["required"] and not q.get("expect_empty"):
            raise ValueError(f"question {q['id']} needs expected evidence or expect_empty")
    for group, flag in (("vectors", "reject"), ("answers", "accept")):
        for case in bundle[group]:
            if not isinstance(case.get(flag), bool):
                raise ValueError(f"{group} {case['id']} {flag} must be boolean")


def pre(value) -> str:
    text = value if isinstance(value, str) else encoded(value)
    return "<pre style='white-space:pre-wrap;overflow-wrap:anywhere'>" + html.escape(text) + "</pre>"


def render_output(stage: str, output) -> str:
    if stage == "bm25" and isinstance(output, list):
        return "<p>검색어: " + html.escape(" · ".join(output)) + "</p>"
    if stage == "retriever" and isinstance(output, dict):
        text = "<h4>모델에 전달할 근거</h4>"
        for evidence in output["evidence"]:
            text += ("<p>" + html.escape(evidence["evidence_id"] + " · 문서 " + evidence["doc_id"]) + "</p>"
                     + pre(evidence["quote"]))
        if not output["evidence"]:
            text += "<p>찾은 근거가 없습니다.</p>"
        text += "<details><summary>검색 순위</summary><ol>"
        text += "".join("<li>" + pre(passage) + "</li>" for passage in output["ranked_passages"])
        text += "</ol></details><details><summary>제외된 후보와 검색 제한</summary>"
        return text + pre({"제외": output["excluded"], "제한": output["limitations"]}) + "</details>"
    if stage == "context":
        text = f"<p>최종 요청 토큰 수: {output['request_tokens']}</p>"
        for message in output["messages"]:
            title = "모델 지시문" if message["role"] == "system" else "질문과 근거"
            text += f"<details><summary>{title}</summary>" + pre(message["content"]) + "</details>"
        return text
    if stage == "data":
        items = output if isinstance(output, list) else output["chunks"]
        text = f"<p>조각 수: {len(items)}</p>"
        for item in items:
            text += pre(item.get("raw_text", item.get("body", "")))
        return text + "<details><summary>위치·토큰·원본 연결</summary>" + pre(output) + "</details>"
    if stage == "generation":
        text = "<p>검사 통과</p>" if output["accepted"] else "<p>답변 거부: " + html.escape(output["reason"]) + "</p>"
        if output["accepted"]:
            text += pre(output["answer"]["summary"])
        return text + "<details><summary>판정 상세</summary>" + pre(output) + "</details>"
    return pre(output)


def render_report(report: dict) -> str:
    counts = {label: sum(r["status"] == label for r in report["rows"])
              for label in ("개선", "악화", "양쪽 실패", "변경됨", "동일", "비교 불가")}
    errors = [f"{side}: {report[side]['error']}" for side in ("before", "after") if report[side].get("error")]
    title = "실행 실패 — 결과를 비교할 수 없습니다" if errors else (
        "악화 또는 실패한 항목을 먼저 확인하세요" if any(counts[k] for k in ("악화", "양쪽 실패", "비교 불가")) else
        "선택한 검사에서 새 실패가 없습니다 — 전체 품질 보장은 아닙니다")
    text = ("<h2>" + title + "</h2><p>" + " · ".join(f"{k} {v}" for k, v in counts.items()) + "</p>"
            "<p>변경됨은 출력 차이이며, 개선 판정이 아닙니다. 실행 시간은 단일 실행의 관찰값입니다. "
            "새 LLM 답변·임베딩 품질·API 비용·DB 운영 동작: 미측정.</p>"
            + pre({"A commit": report["baseline_commit"], "A source": report["before_sha"],
                   "B source": report["after_sha"], "input": report["input_sha"], "saved": report["directory"]}))
    if errors:
        text += pre("\n".join(errors))
    if report["dependency_files_changed"]:
        text += "<p>의존성 파일이 변경되었습니다. 양쪽은 동일한 설치 환경에서 실행했으므로 의존성 변경 효과는 미측정입니다.</p>"
    if report["working_files_changed_during_run"]:
        text += "<p>실행 도중 파일이 바뀌었습니다. B는 실행 시작 시점의 스냅샷입니다. 최신 수정은 다시 실행하세요.</p>"
    text += "<details><summary>실제 코드 차이</summary>" + pre(report["source_diff"] or "코드 차이가 없습니다.") + "</details>"
    return text


def render_stage(report: dict, stage: str) -> str:
    title, description, _ = STAGES[stage]
    out = f"<h3>{title}</h3><p>{description}</p>"
    rows = [r for r in report["rows"] if r["stage"] == stage]
    if not rows:
        return out + "<p>미측정 — 해당 결과가 없습니다.</p>"
    for row in rows:
        out += f"<details{' open' if row['status'] != '동일' else ''}><summary>{html.escape(row['id'])} · {row['status']}</summary>"
        out += "<details><summary>같은 입력</summary>" + pre((row["after"] or row["before"])["input"]) + "</details>"
        out += "<table style='width:100%;table-layout:fixed'><thead><tr><th>A · 수정 전</th><th>B · 수정 후</th></tr></thead><tbody><tr>"
        for side in ("before", "after"):
            value = row[side]
            out += "<td style='vertical-align:top;padding:12px;border:1px solid #aaa'>"
            if value is None:
                out += "결과 없음"
            else:
                out += f"<p>실행: {value['elapsed_ms']} ms</p><ul>"
                out += "".join("<li>" + ("통과 · " if passed else "실패 · ") + html.escape(label) + "</li>"
                               for label, passed in value["checks"].items())
                out += "</ul>" if value["checks"] else "</ul><p>관찰만 수행 — 정답 판정 없음</p>"
                out += pre(value["error"]) if value.get("error") else render_output(stage, value["output"])
            out += "</td>"
        out += "</tr></tbody></table></details>"
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync")
    sub.add_parser("watch")
    run = sub.add_parser("run")
    run.add_argument("--input", type=Path)
    base = sub.add_parser("baseline")
    base.add_argument("--ref", required=True)
    args = parser.parse_args()
    if args.command == "baseline":
        ref = git(ROOT, "rev-parse", "--verify", args.ref + "^{commit}").decode().strip()
        write_atomic(ROOT / BASELINE, encoded({"commit": ref}) + "\n")
        sync()
        print(f"Baseline fixed at {ref}")
    elif args.command == "sync":
        print("Notebook refreshed" if sync() else "Notebook already current")
    elif args.command == "watch":
        watch()
    else:
        report = run_comparison(input_path=args.input)
        print(report["directory"])
        if any(report[s].get("error") for s in ("before", "after")) or any(
                row["status"] in ("악화", "양쪽 실패", "비교 불가") for row in report["rows"]):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
