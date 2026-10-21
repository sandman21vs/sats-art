"""Formato da arte: deitada (16:9, computador) ou em pé (9:16, celular)."""
import asyncio
import importlib
import io

import httpx
import pytest
from PIL import Image

from motor.rede_nos import dimensoes
from servidor.render import ParametroInvalido, Renderizador


def test_dimensoes_usam_o_lado_maior():
    assert dimensoes(1920) == (1920, 1080)
    assert dimensoes(1920, retrato=True) == (1080, 1920)


def test_previa_em_pe_sai_9_por_16():
    r = Renderizador()
    jpg, cfg = r._gerar_bytes({"retrato": True, "seed": 3}, 640, "JPEG")
    assert cfg.retrato and Image.open(io.BytesIO(jpg)).size == (360, 640)
    jpg, _ = r._gerar_bytes({"seed": 3}, 640, "JPEG")
    assert Image.open(io.BytesIO(jpg)).size == (640, 360)


def test_retrato_precisa_ser_booleano():
    with pytest.raises(ParametroInvalido):
        Renderizador.ler_config({"retrato": "sim"})


def test_formato_entra_na_chave_do_cache_da_rede():
    a = Renderizador.ler_config({"retrato": True}).chave_rede()
    b = Renderizador.ler_config({"retrato": False}).chave_rede()
    assert a != b


def test_exportacao_em_pe_tem_nome_e_tamanho_certos(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COINOS_TOKEN", "")
    main = importlib.import_module("servidor.main")
    monkeypatch.setattr(main, "render", Renderizador(1, 8))

    async def chamar():
        t = httpx.ASGITransport(app=main.app, client=("203.0.113.40", 1))
        async with httpx.AsyncClient(transport=t, base_url="http://s") as c:
            return await c.post("/api/exportar", json={"fonte": "sat", "largura": 640, "retrato": True})
    r = asyncio.run(chamar())
    assert r.status_code == 200
    assert r.headers["X-Nome-Arquivo"].endswith("_retrato_s21_360x640.png")
    assert Image.open(io.BytesIO(r.content)).size == (360, 640)
