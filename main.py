from fastapi import FastAPI, UploadFile, File, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import google.generativeai as genai
from pdf2image import convert_from_bytes
import io
import base64
import sqlite3
import json
from datetime import datetime
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

# ── BANCO DE DADOS ──
DB_PATH = "historico.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transcricoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome_arquivo TEXT NOT NULL,
            total_paginas INTEGER NOT NULL,
            transcricao TEXT NOT NULL,
            criado_em TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

init_db()

def salvar_transcricao(nome_arquivo: str, total_paginas: int, paginas: list):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO transcricoes (nome_arquivo, total_paginas, transcricao, criado_em) VALUES (?, ?, ?, ?)",
        (
            nome_arquivo,
            total_paginas,
            json.dumps(paginas, ensure_ascii=False),
            datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        )
    )
    conn.commit()
    conn.close()

def buscar_historico(limite: int = 50):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, nome_arquivo, total_paginas, criado_em FROM transcricoes ORDER BY id DESC LIMIT ?",
        (limite,)
    ).fetchall()
    conn.close()
    return [
        {"id": r[0], "nome_arquivo": r[1], "total_paginas": r[2], "criado_em": r[3]}
        for r in rows
    ]

def buscar_transcricao(id: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, nome_arquivo, total_paginas, transcricao, criado_em FROM transcricoes WHERE id = ?",
        (id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "nome_arquivo": row[1],
        "total_paginas": row[2],
        "paginas": json.loads(row[3]),
        "criado_em": row[4]
    }

def deletar_transcricao(id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM transcricoes WHERE id = ?", (id,))
    conn.commit()
    conn.close()

# ── ROTAS ──
@app.get("/")
def serve_frontend():
    return FileResponse("static/index.html")

@app.post("/transcrever")
async def transcrever_pdf(
    file: UploadFile = File(...),
    x_api_key: str = Header(..., alias="X-Api-Key")
):
    if not x_api_key or len(x_api_key) < 10:
        raise HTTPException(status_code=401, detail="Chave de API inválida.")

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Apenas arquivos PDF são aceitos.")

    try:
        genai.configure(api_key=x_api_key)
        model = genai.GenerativeModel("gemini-1.5-flash-latest")
    except Exception:
        raise HTTPException(status_code=401, detail="Erro ao autenticar com o Gemini. Verifique sua chave de API.")

    try:
        # PDF lido na memória — nunca salvo em disco
        pdf_bytes = await file.read()
        paginas_img = convert_from_bytes(pdf_bytes, dpi=200)
        del pdf_bytes  # descarta imediatamente após conversão
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao processar o PDF: {str(e)}")

    prompt = """Você é um especialista em transcrição de documentos jurídicos e cartorários brasileiros.
Transcreva o conteúdo desta página com máxima fidelidade, preservando:
- Toda a formatação e estrutura do documento
- Nomes, datas, valores e números exatamente como aparecem
- Termos jurídicos e técnicos sem alteração
- Parágrafos e separações originais

Retorne apenas o texto transcrito, sem comentários ou explicações adicionais."""

    transcricao_completa = []

    for i, pagina in enumerate(paginas_img):
        try:
            img_buffer = io.BytesIO()
            pagina.save(img_buffer, format="PNG")
            img_buffer.seek(0)
            img_data = img_buffer.read()

            image_part = {
                "mime_type": "image/png",
                "data": base64.b64encode(img_data).decode("utf-8")
            }

            resposta = model.generate_content([
                {"text": prompt},
                {"inline_data": image_part}
            ])

            transcricao_completa.append({
                "pagina": i + 1,
                "texto": resposta.text.strip()
            })

        except Exception as e:
            transcricao_completa.append({
                "pagina": i + 1,
                "texto": f"[Erro ao transcrever esta página: {str(e)}]"
            })

    # Salva apenas o texto transcrito — o PDF nunca é gravado em disco
    salvar_transcricao(
        nome_arquivo=file.filename,
        total_paginas=len(paginas_img),
        paginas=transcricao_completa
    )

    return {
        "total_paginas": len(paginas_img),
        "paginas": transcricao_completa
    }

@app.get("/historico")
def listar_historico():
    return buscar_historico()

@app.get("/historico/{id}")
def detalhe_transcricao(id: int):
    item = buscar_transcricao(id)
    if not item:
        raise HTTPException(status_code=404, detail="Transcrição não encontrada.")
    return item

@app.delete("/historico/{id}")
def remover_transcricao(id: int):
    deletar_transcricao(id)
    return {"ok": True}
