import streamlit as st
import os, sys, re, io, zipfile, tempfile, subprocess, shutil, csv
from pydub import AudioSegment
from pydub.silence import split_on_silence, detect_nonsilent

# --- FFmpeg SETUP ---
@st.cache_resource
def setup_ffmpeg():
    ffmpeg_cmd = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'
    ffprobe_cmd = 'ffprobe.exe' if os.name == 'nt' else 'ffprobe'
    ffmpeg_path = shutil.which(ffmpeg_cmd)
    ffprobe_path = shutil.which(ffprobe_cmd)
    if not ffmpeg_path or not ffprobe_path:
        st.error("FFmpeg/FFprobe not found. Please install and ensure they're in your PATH.")
        st.stop()
    AudioSegment.converter = ffmpeg_path
    AudioSegment.ffprobe = ffprobe_path
    return ffmpeg_path

# --- UTILITY: In-Memory File ---
class InMemoryFile:
    def __init__(self, name, data):
        self.name = name
        self._data = data
    def read(self):
        return self._data

# --- CUT AUDIO ---
def cut_audio(mp3_bytes, txt_bytes, suffix, progress=None):
    sound = AudioSegment.from_file(io.BytesIO(mp3_bytes), format="mp3")
    chunks = split_on_silence(sound,
                              min_silence_len=1000,
                              silence_thresh=-50,
                              keep_silence=150)
    lines = txt_bytes.decode('utf8', errors='ignore').splitlines()
    buf_zip = io.BytesIO()
    with zipfile.ZipFile(buf_zip, mode="w") as zf:
        total = len(chunks)
        for i, chunk in enumerate(chunks, start=1):
            text_line = re.sub(r'[^\w]', '', lines[i-1].strip()) if i-1 < len(lines) else str(i)
            name = f"{i}-{text_line}-{suffix}.mp3"
            pad = AudioSegment.silent(duration=150)
            combined = pad + chunk + pad
            buf2 = io.BytesIO()
            combined.export(buf2, format="mp3", bitrate="320k")
            buf2.seek(0)
            zf.writestr(name, buf2.getvalue())
            if progress:
                progress(int(i/total*100))
    buf_zip.seek(0)
    return buf_zip

# --- JOIN AUDIO ---
def join_audio(files, mode, progress=None):
    def key(f):
        m = re.search(r"(\d+)", f.name)
        return int(m.group(1)) if m else float('inf')
    files = sorted(files, key=key)
    total = len(files)
    segments = []
    for i, f in enumerate(files, start=1):
        data = f.read()
        sound = AudioSegment.from_file(io.BytesIO(data), format="mp3")
        nons = detect_nonsilent(sound, min_silence_len=25, silence_thresh=-40)
        if nons:
            start = max(0, nons[0][0] - 100)
            end = min(len(sound), nons[-1][1] + 100)
            sound = sound[start:end]
        segments.append(sound)
        if progress:
            progress(int(i/total*50))
    combined = AudioSegment.empty()
    for i, seg in enumerate(segments, start=1):
        combined += seg
        combined += AudioSegment.silent(duration=1000) if mode == 'SAI' else AudioSegment.silent(duration=len(seg) * 2)
        if progress:
            progress(50 + int(i/total*50))
    if mode == 'SAI':
        combined = combined.apply_gain(-3 - combined.max_dBFS)
    out_buf = io.BytesIO()
    combined.export(out_buf, format="mp3", bitrate="320k")
    out_buf.seek(0)
    return out_buf

# --- NORMALIZE AUDIO ---
def normalize_audio(input_bytes, target_lufs, peak_db, progress=None):
    setup_ffmpeg()
    audio = AudioSegment.from_file(io.BytesIO(input_bytes))
    tmp_in = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
    audio.export(tmp_in.name, format='wav'); tmp_in.close()
    if progress: progress(10)
    tmp_out = tempfile.NamedTemporaryFile(suffix='.wav', delete=False); tmp_out.close()
    if progress: progress(20)
    cmd = [AudioSegment.converter, '-y', '-i', tmp_in.name,
           '-af', f"loudnorm=I={target_lufs}:TP={peak_db}:measured_TP={peak_db}",
           '-ar', '48000', '-c:a', 'pcm_s16le', tmp_out.name]
    subprocess.run(cmd, check=True, capture_output=True)
    if progress: progress(70)
    norm = AudioSegment.from_wav(tmp_out.name)
    out_buf = io.BytesIO(); norm.export(out_buf, format="mp3", bitrate="320k"); out_buf.seek(0)
    if progress: progress(100)
    os.unlink(tmp_in.name); os.unlink(tmp_out.name)
    return out_buf

# --- GENERATE ANKI CSV ---
def generate_anki_csv(csv_bytes, files, progress=None):
    text = csv_bytes.decode('utf8', errors='ignore').splitlines()
    rows = list(csv.reader(text, delimiter='\t'))
    def key(f):
        m = re.search(r"(\d+)", f.name)
        return int(m.group(1)) if m else float('inf')
    files = sorted(files, key=key)
    total = len(rows)
    if len(rows) != len(files):
        raise ValueError(f"Row count ({len(rows)}) != file count ({len(files)})")
    buf = io.StringIO(); writer = csv.writer(buf, delimiter='\t', lineterminator='\n')
    for i, row in enumerate(rows, start=1):
        writer.writerow(row + [f"[sound:{files[i-1].name}]"])
        if progress: progress(int(i/total*100))
    return io.BytesIO(buf.getvalue().encode('utf8'))

# --- STREAMLIT UI ---
st.set_page_config(page_title="MB Audio Tools", layout="centered")
st.title("🎧 MB Audio Processing")
# Initialize FFmpeg
setup_ffmpeg()

tabs = st.tabs(["Normalize", "Cut", "Join", "Anki CSV"])

# Tab 1: Normalize
with tabs[0]:
    st.header("1. Normalize Audio")
    inp = st.file_uploader("Upload MP3 or WAV to normalize", type=["mp3","wav"], key="norm_in")
    target = st.number_input("Target LUFS", value=-18.0, key="norm_lufs")
    peak = st.number_input("Peak dBTP", value=-3.0, key="norm_peak")
    if st.button("Run Normalize", key="norm_run") and inp:
        progress = st.progress(0)
        try:
            out_buf = normalize_audio(inp.read(), target, peak, progress=progress.progress)
            st.success("Normalization complete!")
            st.session_state['normalized_audio'] = out_buf.getvalue()
            st.download_button("Download normalized MP3", data=out_buf,
                                file_name="normalized.mp3", mime="audio/mp3")
        except Exception as e:
            st.error(str(e))

# Tab 2: Cut
with tabs[1]:
    st.header("2. Cut Audio into Chunks")
    use_norm = st.session_state.get('normalized_audio') is not None and st.checkbox(
        "Use normalized output from Tab 1", key="use_norm")
    mp3_data = st.session_state['normalized_audio'] if use_norm else None
    if not mp3_data:
        mp3_file = st.file_uploader("Upload MP3 to cut", type="mp3", key="cut_in")
        mp3_data = mp3_file.read() if mp3_file else None
    txt_file = st.file_uploader("Upload Text (.txt)", type="txt", key="cut_txt")
    txt_data = txt_file.read() if txt_file else None
    suffix = st.text_input("Suffix-Abbr [Daily-P1-Diwu]", value="Theme-Part-User", key="cut_suf")
    if st.button("Cut Audio", key="cut_run") and mp3_data and txt_data:
        progress = st.progress(0)
        zip_buf = cut_audio(mp3_data, txt_data, suffix, progress=progress.progress)
        st.success("Cutting complete!")
        st.session_state['cut_zip'] = zip_buf.getvalue()
        st.download_button("Download chunks ZIP", data=zip_buf,
                            file_name=f"chunks-{suffix}.zip", mime="application/zip")

# Tab 3: Join
with tabs[2]:
    st.header("3. Join Audio Chunks")
    suffix_join = st.text_input("Suffix-Full [Daily_Routines-Part2-Diwu]", value="Theme-Part-User", key="join_suf")
    use_cut = st.session_state.get('cut_zip') is not None and st.checkbox(
        "Use chunks from Tab 2", key="use_cut")
    if use_cut:
        zb = io.BytesIO(st.session_state['cut_zip'])
        with zipfile.ZipFile(zb) as zf:
            chunk_files = [InMemoryFile(name, zf.read(name)) for name in zf.namelist()]
    else:
        chunk_files = st.file_uploader("Select MP3 Chunks to join", type="mp3",
                                       accept_multiple_files=True, key="join_in")
    mode = st.radio("Mode", ["SAI","LAR"], key="join_mode")
    if st.button("Join", key="join_run") and chunk_files:
        progress = st.progress(0)
        joined_buf = join_audio(chunk_files, mode, progress=progress.progress)
        st.success("Join complete!")
        output_name = f"{mode}-{suffix_join}.mp3"
        st.download_button("Download joined MP3", data=joined_buf,
                            file_name=output_name, mime="audio/mp3")

# Tab 4: Anki CSV
with tabs[3]:
    st.header("4. Generate Anki CSV")
    csv_in = st.file_uploader("Upload TSV/CSV (3 cols)", type=["csv","tsv","txt"], key="anki_in")
    uploaded = st.file_uploader("Upload Audio Files to memory", type=["mp3","wav","ogg","flac"],
                                 accept_multiple_files=True, key="anki_upload")
    if uploaded:
        files_mem = [InMemoryFile(f.name, f.read()) for f in uploaded]
        st.session_state['anki_audio_files'] = files_mem
        st.success(f"{len(files_mem)} audio files loaded into memory")
    audio_files = st.session_state.get('anki_audio_files', [])
    if audio_files:
        st.write(f"{len(audio_files)} audio files ready for use")
    if st.button("Generate TSV", key="anki_run"):
        if not csv_in:
            st.error("Please upload input CSV/TSV first")
        elif not audio_files:
            st.error("Please upload audio files to memory")
        else:
            progress = st.progress(0)
            try:
                tsv_buf = generate_anki_csv(csv_in.read(), audio_files, progress=progress.progress)
                st.success("Anki CSV ready!")
                st.download_button("Download TSV", data=tsv_buf,
                                    file_name="anki_output.tsv",
                                    mime="text/tab-separated-values")
            except Exception as e:
                st.error(str(e))
