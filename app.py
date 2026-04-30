import os
import json
import uuid
import subprocess
import threading
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import uvicorn
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR.mkdir(exist_ok=True)

jobs = {}

SIZE_MAP = {
    "pequeno":  (40, 28),
    "medio":    (52, 36),
    "grande":   (62, 42),
    "extra":    (78, 55),
}

# (Alignment, MarginV)  Alignment: 8=top-center, 5=mid-center, 2=bottom-center
POSITION_MAP = {
    "topo":   (8, 40),
    "centro": (5, 0),
    "baixo":  (2, 80),
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def hex_to_ass(hex_color: str) -> str:
    """Convert #RRGGBB to ASS &H00BBGGRR."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return "&H00FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def get_video_info(video_path: str) -> dict:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-show_entries", "format=duration",
        "-of", "json", video_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(r.stdout)
    s = data["streams"][0]
    return {"width": s["width"], "height": s["height"], "duration": float(data["format"]["duration"])}


def ass_time(t: float) -> str:
    h, m, s = int(t // 3600), int((t % 3600) // 60), int(t % 60)
    cs = int((t % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


# ─── Pipeline ─────────────────────────────────────────────────────────────────

def extract_audio(video_path: str, out_dir: Path) -> str:
    audio_path = str(out_dir / "audio.wav")
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", audio_path
    ], capture_output=True, check=True)
    return audio_path


def transcribe_audio(audio_path: str) -> list:
    import google.generativeai as genai
    import re, time

    genai.configure(api_key=os.getenv("GEMINI_KEY"))

    uploaded = genai.upload_file(path=audio_path, mime_type="audio/wav")
    for _ in range(30):
        f = genai.get_file(uploaded.name)
        if f.state.name != "PROCESSING":
            break
        time.sleep(2)

    model = genai.GenerativeModel("gemini-2.5-flash")
    prompt = (
        "Transcreva este áudio em português do Brasil. "
        "Retorne SOMENTE JSON válido, sem markdown, sem explicações:\n"
        '{"segments":[{"start":0.0,"end":3.2,"text":"..."},...]}\n'
        "Regras: start/end em segundos (float), 5 a 15 palavras por segmento, cubra todo o áudio sem lacunas."
    )
    response = model.generate_content([uploaded, prompt])

    try:
        genai.delete_file(uploaded.name)
    except Exception:
        pass

    text = response.text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    data = json.loads(text)
    return [
        {"index": i, "start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip()}
        for i, s in enumerate(data.get("segments", []))
    ]


def find_speech_intervals(segments: list, min_gap=0.5, padding=0.05) -> list:
    if not segments:
        return []
    intervals = [(max(0, s["start"] - padding), s["end"] + padding) for s in segments]
    merged = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start - merged[-1][1] < min_gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [tuple(i) for i in merged]


def cut_silences(video_path: str, intervals: list, out_dir: Path) -> str:
    if not intervals:
        return video_path
    output_path = str(out_dir / "cut.mp4")
    n = len(intervals)
    parts = []
    for i, (s, e) in enumerate(intervals):
        parts.append(f"[0:v]trim={s:.3f}:{e:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim={s:.3f}:{e:.3f},asetpts=PTS-STARTPTS[a{i}]")
    concat = "".join(f"[v{i}][a{i}]" for i in range(n))
    parts.append(f"{concat}concat=n={n}:v=1:a=1[vout][aout]")
    script = out_dir / "cut_filter.txt"
    script.write_text(";\n".join(parts))
    result = subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-filter_complex_script", str(script),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k", output_path
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg corte falhou:\n{result.stderr[-2000:]}")
    return output_path


def remap_timestamps(segments: list, intervals: list) -> list:
    cum = 0
    idata = []
    for s, e in intervals:
        idata.append((s, e, cum))
        cum += e - s
    result = []
    for seg in segments:
        new_s = new_e = None
        for s, e, offset in idata:
            if s <= seg["start"] <= e and new_s is None:
                new_s = offset + (seg["start"] - s)
            if s <= seg["end"] <= e and new_e is None:
                new_e = offset + (seg["end"] - s)
        if new_s is None:
            continue
        if new_e is None:
            new_e = new_s + (seg["end"] - seg["start"])
        result.append({**seg, "start": round(max(0, new_s), 3), "end": round(max(0, new_e), 3)})
    return result


def segments_to_word_entries(segments: list, chunk_size: int = 3) -> list:
    entries = []
    for seg in segments:
        words = seg["text"].split()
        if not words:
            continue
        duration = seg["end"] - seg["start"]
        word_dur = duration / len(words)
        for i in range(0, len(words), chunk_size):
            chunk = words[i:i + chunk_size]
            start = seg["start"] + i * word_dur
            end = min(seg["start"] + (i + len(chunk)) * word_dur, seg["end"])
            entries.append({"start": start, "end": end, "text": " ".join(chunk)})
    return entries


def build_ass(entries: list, width: int, height: int, options: dict) -> str:
    is_vertical = height > width
    size_key = options.get("size", "grande")
    size_v, size_h = SIZE_MAP.get(size_key, SIZE_MAP["grande"])
    font_size = size_v if is_vertical else size_h

    pos_key = options.get("position", "baixo")
    alignment, margin_v = POSITION_MAP.get(pos_key, POSITION_MAP["baixo"])

    font = options.get("font", "Arial")
    primary = hex_to_ass(options.get("color", "#ffffff"))
    outline = hex_to_ass(options.get("outline", "#000000"))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{font_size},{primary},&H000000FF,{outline},&H00000000,-1,0,0,0,100,100,0,0,1,4,0,{alignment},20,20,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    an_tag = "{\\an" + str(alignment) + "}"
    events = "\n".join(
        f"Dialogue: 0,{ass_time(e['start'])},{ass_time(e['end'])},Default,,0,0,0,,{an_tag}{e['text'].strip()}"
        for e in entries
        if e.get("text", "").strip()
    )
    return header + events + "\n"


def render_video(cut_video: str, segments: list, out_dir: Path, options: dict) -> str:
    output_path = str(out_dir / "final.mp4")
    info = get_video_info(cut_video)
    w, h = info["width"], info["height"]

    chunk_size = int(options.get("chunk", 3))
    entries = segments_to_word_entries(segments, chunk_size=chunk_size)

    ass_content = build_ass(entries, w, h, options)
    ass_path = out_dir / "subs.ass"
    ass_path.write_text(ass_content, encoding="utf-8")

    result = subprocess.run([
        "ffmpeg", "-y", "-i", cut_video,
        "-vf", "ass=subs.ass",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "copy", output_path
    ], capture_output=True, text=True, cwd=str(out_dir))

    if result.returncode != 0:
        raise RuntimeError(f"Render falhou:\n{result.stderr[-2000:]}")
    return output_path


# ─── Background workers ────────────────────────────────────────────────────────

def run_pipeline(job_id: str):
    job = jobs[job_id]
    out_dir = Path(job["out_dir"])

    def upd(step, msg, pct):
        jobs[job_id].update({"step": step, "message": msg, "progress": pct})

    try:
        upd(1, "Extraindo áudio...", 15)
        audio = extract_audio(job["video_path"], out_dir)

        upd(2, "Transcrevendo com Gemini...", 35)
        segments = transcribe_audio(audio)

        upd(3, "Cortando silêncios...", 65)
        intervals = find_speech_intervals(segments)
        cut_path = cut_silences(job["video_path"], intervals, out_dir)
        segments = remap_timestamps(segments, intervals)

        jobs[job_id].update({
            "status": "transcribed",
            "step": 4,
            "message": "Transcrição pronta! Corrija se necessário e gere o vídeo.",
            "progress": 100,
            "cut_path": cut_path,
            "segments": segments,
        })

    except Exception as e:
        jobs[job_id].update({"status": "error", "message": str(e), "progress": 0})


def run_render(job_id: str, options: dict):
    job = jobs[job_id]
    out_dir = Path(job["out_dir"])
    try:
        jobs[job_id].update({"status": "rendering", "message": "Renderizando...", "progress": 50})
        final = render_video(job["cut_path"], job["segments"], out_dir, options)
        jobs[job_id].update({"status": "done", "message": "Vídeo pronto!", "progress": 100, "final_path": final})
    except Exception as e:
        jobs[job_id].update({"status": "error", "message": str(e), "progress": 0})


# ─── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI(title="ECO Captions")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())[:8]
    out_dir = UPLOAD_DIR / job_id
    out_dir.mkdir(parents=True)
    video_path = str(out_dir / f"input{Path(file.filename).suffix}")
    with open(video_path, "wb") as f:
        f.write(await file.read())
    jobs[job_id] = {
        "status": "uploaded", "step": 0, "message": "Vídeo recebido", "progress": 0,
        "video_path": video_path, "out_dir": str(out_dir)
    }
    return {"job_id": job_id}


@app.post("/process/{job_id}")
def process(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job não encontrado")
    jobs[job_id]["status"] = "processing"
    threading.Thread(target=run_pipeline, args=(job_id,), daemon=True).start()
    return {"ok": True}


@app.get("/status/{job_id}")
def status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job não encontrado")
    job = jobs[job_id]
    return {
        "status": job["status"],
        "step": job.get("step", 0),
        "message": job.get("message", ""),
        "progress": job.get("progress", 0),
    }


@app.get("/transcript/{job_id}")
def get_transcript(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404)
    return {"segments": jobs[job_id].get("segments", [])}


class TranscriptUpdate(BaseModel):
    segments: list


@app.put("/transcript/{job_id}")
def update_transcript(job_id: str, body: TranscriptUpdate):
    if job_id not in jobs:
        raise HTTPException(404)
    jobs[job_id]["segments"] = body.segments
    return {"ok": True}


class RenderRequest(BaseModel):
    color: Optional[str] = "#ffffff"
    outline: Optional[str] = "#000000"
    font: Optional[str] = "Arial"
    size: Optional[str] = "grande"
    position: Optional[str] = "baixo"
    chunk: Optional[int] = 3


@app.post("/render/{job_id}")
async def render(job_id: str, request: Request):
    if job_id not in jobs:
        raise HTTPException(404, "Job não encontrado")
    if jobs[job_id]["status"] not in ("transcribed", "done"):
        raise HTTPException(400, "Processe o vídeo primeiro")
    try:
        data = await request.json()
        req = RenderRequest(**data)
    except Exception:
        req = RenderRequest()
    jobs[job_id]["status"] = "transcribed"
    options = req.model_dump()
    threading.Thread(target=run_render, args=(job_id, options), daemon=True).start()
    return {"ok": True}


@app.get("/download/{job_id}")
def download(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404)
    job = jobs[job_id]
    if job["status"] != "done":
        raise HTTPException(400, "Vídeo ainda não pronto")
    path = job.get("final_path")
    if not path or not Path(path).exists():
        raise HTTPException(404, "Arquivo não encontrado")
    return FileResponse(path, media_type="video/mp4", filename=f"eco-captions_{job_id}.mp4",
                        headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
