from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import json
import requests
import psycopg
import time
from dotenv import load_dotenv
import os
import uuid
from google import genai
from google.genai import types
from supabase import create_client

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

def transcribe_with_gnani(file_path, filename, content_type):
    config = {
        "model": "gnani-prisma-v2.5",
        "language_code": "en-IN",
        "mode": "transcribe",
        "with_diarization": False,
        "is_multi_channel": False
    }

    with open(file_path, "rb") as audio_file:
        files = {
            "files": (
                filename,
                audio_file,
                content_type or "application/octet-stream"
            )
        }

        data = {
            "config": json.dumps(config)
        }

        response = requests.post(
            "https://api.vachana.ai/stt/v3/batch/jobs",
            headers={"X-API-Key-ID": gnani_api_key},
            files=files,
            data=data,
            timeout=60
        )

    response.raise_for_status()

    job = response.json()
    job_id = job["job_id"]

    while True:
        start_response = requests.post(
            f"https://api.vachana.ai/stt/v3/batch/jobs/{job_id}/start",
            headers={"X-API-Key-ID": gnani_api_key},
            timeout=60
        )

        if start_response.status_code == 429:
            print("Gnani rate limit while starting job. Waiting 15 seconds...")
            time.sleep(15)
            continue

        start_response.raise_for_status()
        break

    # Wait for Gnani to finish processing
    # Wait for Gnani to finish processing
    while True:
        status_response = requests.get(
            f"https://api.vachana.ai/stt/v3/batch/jobs/{job_id}",
            headers={"X-API-Key-ID": gnani_api_key},
            timeout=60
        )

        if status_response.status_code == 429:
            print("Gnani rate limit reached while checking status. Waiting 15 seconds...")
            time.sleep(15)
            continue

        status_response.raise_for_status()

        status_data = status_response.json()
        status = status_data["status"]

        print(f"Gnani job {job_id}: {status}")

        if status == "COMPLETED":
            break

        if status in ["FAILED", "CANCELLED"]:
            raise Exception(f"Gnani job failed: {status}")

        time.sleep(10)

    # Get completed file
    while True:
        files_response = requests.get(
            f"https://api.vachana.ai/stt/v3/batch/jobs/{job_id}/files",
            headers={"X-API-Key-ID": gnani_api_key},
            params={"status": "COMPLETED"},
            timeout=60
        )

        if files_response.status_code == 429:
            print("Gnani rate limit while getting transcript. Waiting 15 seconds...")
            time.sleep(15)
            continue

        files_response.raise_for_status()
        break

    completed_files = files_response.json()["data"]

    if not completed_files:
        raise Exception("Gnani completed the job but returned no transcript.")

    transcript_url = completed_files[0]["transcript_url"]

    # Download transcript
    transcript_response = requests.get(
        transcript_url,
        timeout=60
    )

    transcript_response.raise_for_status()

    transcript_data = transcript_response.json()

    return transcript_data["full_transcript"]

def generate_summary(transcript):
    for attempt in range(5):
        try:
            response = gemini_client.models.generate_content(
                model="gemini-3.8-flash",
                contents=f"""
Summarize the following audio transcript clearly and concisely.

Give:
1. A short overview
2. The main points discussed
3. Important action items or decisions, if any

Transcript:
{transcript}
"""
            )

            return response.text

        except Exception as e:
            print(f"Gemini attempt {attempt + 1} failed: {e}")

            if attempt == 4:
                raise

            wait_time = 2 ** attempt
            print(f"Retrying Gemini in {wait_time} seconds...")
            time.sleep(wait_time)
def process_recording(recording_id, file_path, filename,content_type):
    try:
        # Mark as transcribing
        conn = get_db_connection()

        cursor = conn.cursor()
        cursor.execute(
            "UPDATE audio_notes SET status = %s WHERE id = %s",
            ("transcribing", recording_id)
        )
        conn.commit()
        cursor.close()
        conn.close()

        # Transcribe with Gnani
        transcript = transcribe_with_gnani(
         file_path,
        filename,
        content_type
        )

        # Save transcript first
        conn = get_db_connection()

        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE audio_notes
            SET transcript = %s
            WHERE id = %s
            """,
            (transcript, recording_id)
        )

        conn.commit()
        cursor.close()
        conn.close()


        # Mark as summarizing
        conn = get_db_connection()

        cursor = conn.cursor()

        cursor.execute(
            "UPDATE audio_notes SET status = %s WHERE id = %s",
            ("summarizing", recording_id)
        )

        conn.commit()
        cursor.close()
        conn.close()
        
        # Generate summary using Gemini
        summary = generate_summary(transcript)

        # Save summary and mark as completed
        conn = get_db_connection()

        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE audio_notes
            SET status = %s,
                summary = %s
            WHERE id = %s
            """,
            ("completed", summary, recording_id)
        )

        conn.commit()
        cursor.close()
        conn.close()

        print(f"Recording {recording_id} completed successfully.")

        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"Temporary file deleted: {file_path}")

    except Exception as e:
        print(f"Processing failed: {e}")

        conn = get_db_connection()

        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE audio_notes
            SET status = %s,
                error_message = %s
            WHERE id = %s
            """,
            ("failed", str(e), recording_id)
        )

        conn.commit()
        cursor.close()
        conn.close()

        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"Temporary file deleted: {file_path}")


app = FastAPI()

load_dotenv()

def get_db_connection():
    return psycopg.connect(os.getenv("DATABASE_URL"))

supabase_url = os.getenv("SUPABASE_URL")
supabase_service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase_bucket = os.getenv("SUPABASE_BUCKET")

supabase = create_client(
    supabase_url,
    supabase_service_role_key
)

gnani_api_key = os.getenv("GNANI_API_KEY")

print("Gnani API key loaded:", bool(gnani_api_key))

gemini_api_key = os.getenv("GEMINI_API_KEY")

gemini_client = genai.Client(
    api_key=gemini_api_key,
    http_options=types.HttpOptions(
        retry_options=types.HttpRetryOptions(
            attempts=5,
            initial_delay=1,
            max_delay=30,
            exp_base=2,
            jitter=1,
            http_status_codes=[408, 429, 500, 502, 503, 504]
        )
    )
)

print("Gemini API key loaded:", bool(gemini_api_key))
# conn = get_db_connection()

# print("Database connected successfully!")
# conn.close()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "message": "AudioNotes backend is running!"
    }


@app.post("/upload")
async def upload_audio(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...)
):
    # Validate audio file type
    allowed_types = {
        "audio/mpeg",
        "audio/wav",
        "audio/x-wav",
        "audio/mp4",
        "audio/x-m4a",
        "audio/webm",
        "audio/ogg",
        "audio/flac",
    }

    file_extension = os.path.splitext(file.filename or "")[1].lower()

    allowed_extensions = {
        ".mp3",
        ".wav",
        ".m4a",
        ".mp4",
        ".webm",
        ".ogg",
        ".flac",
    }

    if file.content_type not in allowed_types and file_extension not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Please upload an audio file."
        )
    # 1. Save the audio file
    upload_folder = "uploads"
    os.makedirs(upload_folder, exist_ok=True)

    original_filename = os.path.basename(file.filename)

    file_extension = os.path.splitext(original_filename)[1].lower()

    storage_filename = f"{uuid.uuid4()}{file_extension}"

    file_path = os.path.join(upload_folder, storage_filename)

    file_size = 0

    with open(file_path, "wb") as buffer:
        while True:
            chunk = await file.read(1024 * 1024)

            if not chunk:
                break

            file_size += len(chunk)

            if file_size > MAX_FILE_SIZE:
                buffer.close()
                os.remove(file_path)

                raise HTTPException(
                    status_code=413,
                    detail="File is too large. Maximum allowed size is 200 MB."
                )

            buffer.write(chunk)
    # 2. Upload the audio file to Supabase Storage
        storage_path = f"recordings/{storage_filename}"

        with open(file_path, "rb") as audio_file:
            supabase.storage.from_(supabase_bucket).upload(
                path=storage_path,
                file=audio_file,
                file_options={
                    "content-type": file.content_type
                }
            )

        print(f"Uploaded to Supabase: {storage_path}")

    # 3. Save information about the upload in PostgreSQL
    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO audio_notes (filename, storage_path, status)
        VALUES (%s, %s, %s)
        RETURNING id;
        """,
        (file.filename, storage_path, "uploaded")
    )

    recording_id = cursor.fetchone()[0]



    conn.commit()
    cursor.close()
    conn.close()

    background_tasks.add_task(
    process_recording,
    recording_id,
    file_path,
    file.filename,
    file.content_type
)

    return {
        "message": "File uploaded successfully",
        "id": recording_id,
        "filename": file.filename,
        "status": "uploaded"
    }


import requests


@app.get("/recordings/{recording_id}")
def get_recording(recording_id: int):
    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, filename, status, transcript, summary, error_message
        FROM audio_notes
        WHERE id = %s
        """,
        (recording_id,)
    )

    recording = cursor.fetchone()

    cursor.close()
    conn.close()

    if recording is None:
        return {"error": "Recording not found"}

    return {
        "id": recording[0],
        "filename": recording[1],
        "status": recording[2],
        "transcript": recording[3],
        "summary": recording[4],
        "error_message": recording[5]
    }
@app.get("/recordings")
def get_recordings():
    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, filename, status, created_at
        FROM audio_notes
        ORDER BY created_at DESC
        """
    )

    recordings = cursor.fetchall()

    cursor.close()
    conn.close()

    return [
        {
            "id": recording[0],
            "filename": recording[1],
            "status": recording[2],
            "created_at": recording[3]
        }
        for recording in recordings
    ]