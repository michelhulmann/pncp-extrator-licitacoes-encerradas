#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PNCP GUI – Listar licitações ENCERRADAS do PNCP com:
  • Período por ANO (01/01..31/12 automaticamente) ou datas Início/Fim
  • Filtros: modalidade, abrangência (municipal/estadual/federal/distrital), UF=BR, IBGE opcional
  • Paginação fixa em 50 itens/página
  • Retomada por página inicial e parada opcional em página final
  • Checkpoint configurável: gera CSV/CSV_BR a cada N páginas
  • Progresso por página "X/Y" + ETA simples
  • Exporta TODOS os campos (flatten) e trata números p/ Excel/Sheets (CSV padrão com ponto; CSV_BR com vírgula)

Dependências:
    pip install requests python-dateutil
"""

import sys
import os
import csv
import time
import json
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, date

import threading
import tkinter as tk
from tkinter import ttk, messagebox

import requests
from requests.exceptions import HTTPError, RequestException, ConnectionError, Timeout
from dateutil import tz

# ---------------------------- Config ----------------------------

BASE_URL = "https://pncp.gov.br/api/consulta"
ENDPOINT = "/v1/contratacoes/publicacao"
PAGE_SIZE = 50
DEFAULT_CHECKPOINT_PAGES = 50
MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 2.0

MODALIDADES = {
    1: "Leilão - Eletrônico",
    2: "Diálogo Competitivo",
    3: "Concurso",
    4: "Concorrência - Eletrônica",
    5: "Concorrência - Presencial",
    6: "Pregão - Eletrônico",
    7: "Pregão - Presencial",
    8: "Dispensa de Licitação",
    9: "Inexigibilidade",
    10: "Manifestação de Interesse",
    11: "Pré-qualificação",
    12: "Credenciamento",
    13: "Leilão - Presencial",
}
ABRANGENCIAS = ["municipal", "estadual", "federal", "distrital"]

NUMERIC_KEYS_HINTS = {
    "valor", "valortotal", "valortotalestimado", "valortotalhomologado",
    "quantidade", "ano", "numero", "cnpj"
}

# ---------------------------- Núcleo de consulta ----------------------------

def _brasil_today_date() -> date:
    br_tz = tz.gettz("America/Sao_Paulo")
    return datetime.now(br_tz).date()

def _only_date_from_api(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.date()
        except Exception:
            return None

def is_encerrada(rec: Dict[str, Any], today_brt: date) -> bool:
    """
    Encerrada se:
      a) dataEncerramentoProposta (apenas data) <= hoje_BRT; OU
      b) valorTotalHomologado > 0; OU
      c) situacaoCompraNome ∈ {Revogada, Anulada, Suspensa}
    """
    situacao = (rec.get("situacaoCompraNome") or "").strip().lower()
    v_hom = rec.get("valorTotalHomologado") or 0

    encerramento_str = rec.get("dataEncerramentoProposta")
    enc_data_ok = False
    dt_date = _only_date_from_api(encerramento_str)
    if dt_date is not None:
        enc_data_ok = dt_date <= today_brt

    enc_sit = situacao in {"revogada", "anulada", "suspensa"}
    try:
        enc_hom = float(v_hom) > 0.0
    except Exception:
        enc_hom = False

    return enc_data_ok or enc_sit or enc_hom

def fetch_page(session: requests.Session, params: dict, page: int, timeout_s: int = 90) -> dict:
    """
    Busca uma página com retry/backoff para resiliência a ConnectionReset/Timeout.
    """
    p = params.copy()
    p["pagina"] = page
    p["tamanhoPagina"] = PAGE_SIZE
    url = f"{BASE_URL}{ENDPOINT}"

    attempt = 0
    while True:
        try:
            r = session.get(url, params=p, headers={"accept": "*/*"}, timeout=timeout_s)
            r.raise_for_status()
            return r.json()
        except (ConnectionError, Timeout, RequestException) as e:
            attempt += 1
            if attempt >= MAX_RETRIES:
                raise
            wait = RETRY_BACKOFF_SECONDS * attempt
            print(f"[warn] Falha na página {page} ({type(e).__name__}). Tentando de novo em {wait:.1f}s...")
            time.sleep(wait)

def run_query_stream(params: dict,
                     esfera_alvo: Optional[str],
                     start_page: int,
                     end_page: Optional[int],
                     quiet: bool = False):
    """
    Gerador que emite (page_number, total_paginas_hint, registros_filtrados_da_pagina).
    Encerra quando:
      • totalPaginas (se informado pela API) é atingido; OU
      • a página veio vazia/curta (< PAGE_SIZE); OU
      • end_page foi atingida.
    """
    session = requests.Session()
    today_brt = _brasil_today_date()
    page = start_page
    total_paginas_hint = None

    while True:
        data = fetch_page(session, params, page)
        registros = data.get("data") or data.get("Data") or data
        if not isinstance(registros, list):
            try:
                registros = list(registros)
            except Exception:
                registros = []

        if page == start_page:
            total_paginas_hint = data.get("totalPaginas")

        # aplica filtros (esfera + encerrada)
        filtrados = []
        for rec in registros:
            esfera = None
            try:
                esfera = rec.get("orgaoEntidade", {}).get("esferaId")
            except Exception:
                pass
            if esfera_alvo and esfera and esfera != esfera_alvo:
                continue
            if is_encerrada(rec, today_brt):
                filtrados.append(rec)

        yield page, total_paginas_hint, filtrados

        # parada por página final
        if end_page is not None and page >= end_page:
            break
        # parada por pista de totalPaginas
        if total_paginas_hint is not None and page >= int(total_paginas_hint):
            break
        # parada por lista vazia (ou página “curta”)
        if not registros or len(registros) < PAGE_SIZE:
            break

        page += 1

# ---------------------------- Flatten e CSV ----------------------------

def flatten_dict(d: Dict[str, Any], parent_key: str = "", sep: str = ".") -> Dict[str, Any]:
    items: Dict[str, Any] = {}
    for k, v in (d or {}).items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else str(k)
        if isinstance(v, dict):
            items.update(flatten_dict(v, new_key, sep=sep))
        elif isinstance(v, list):
            for idx, elem in enumerate(v):
                idx_key = f"{new_key}[{idx}]"
                if isinstance(elem, (dict, list)):
                    items.update(flatten_dict({"_": elem}, idx_key, sep=sep))
                else:
                    items[idx_key] = elem
        else:
            items[new_key] = v
    return items

def looks_numeric(key: str, value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    k = key.lower().replace("_", "").replace(".", "")
    if any(h in k for h in NUMERIC_KEYS_HINTS):
        try:
            float(str(value).replace(",", "."))
            return True
        except Exception:
            return False
    return False

def to_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        # normaliza vírgula como decimal
        return float(s.replace(".", "").replace(",", ".")) if s.count(",") == 1 and s.count(".") > 1 else float(s.replace(",", "."))
    except Exception:
        return None

def write_csv_us(rows_flat: List[Dict[str, Any]], outfile: str) -> str:
    fieldnames = sorted({k for r in rows_flat for k in r.keys()})
    with open(outfile, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows_flat:
            row = {}
            for k in fieldnames:
                v = r.get(k)
                if looks_numeric(k, v):
                    n = to_number(v)
                    row[k] = (None if n is None else n)
                else:
                    row[k] = v
            w.writerow(row)
    return outfile

def write_csv_br(rows_flat: List[Dict[str, Any]], outfile: str) -> str:
    fieldnames = sorted({k for r in rows_flat for k in r.keys()})
    with open(outfile, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(fieldnames)
        for r in rows_flat:
            row = []
            for k in fieldnames:
                v = r.get(k)
                if looks_numeric(k, v):
                    n = to_number(v)
                    if n is None:
                        row.append("")
                    else:
                        s = f"{n:.2f}".replace(".", ",")  # vírgula como decimal
                        row.append(s)
                else:
                    row.append("" if v is None else str(v))
            w.writerow(row)
    return outfile

def save_chunk(rows_raw: List[Dict[str, Any]],
               base: str,
               chunk_start_page: int,
               chunk_end_page: int,
               only_us: bool,
               only_br: bool) -> List[str]:
    """
    Salva um chunk (lista bruta) em CSV e CSV_BR (flatten). Retorna caminhos criados.
    """
    if not rows_raw:
        return []
    rows_flat = [flatten_dict(r) for r in rows_raw]
    outputs = []
    # CSV padrão
    if not only_br:
        path_us = f"{base}_p{chunk_start_page}-{chunk_end_page}.csv"
        write_csv_us(rows_flat, path_us)
        outputs.append(os.path.abspath(path_us))
    # CSV_BR
    if not only_us:
        path_br = f"{base}_p{chunk_start_page}-{chunk_end_page}_BR.csv"
        write_csv_br(rows_flat, path_br)
        outputs.append(os.path.abspath(path_br))
    return outputs

# ---------------------------- Helpers ----------------------------

def fmt_eta(seconds: float) -> str:
    if seconds is None or seconds < 0:
        return "?"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

# ---------------------------- Execução para GUI ----------------------------

def run_pncp_job(
    params: Dict[str, Any],
    esfera_alvo: Optional[str],
    start_page: int,
    end_page: Optional[int],
    checkpoint_pages: int,
    base: str,
    only_us: bool,
    only_br: bool,
    log,
) -> Dict[str, Any]:
    """
    Executa a consulta ao PNCP de forma semelhante ao main() do CLI,
    mas usando callback de log (para GUI) e retornando um dict com resultados.
    """
    total_registros = 0
    outputs_all: List[str] = []
    buffer_raw: List[Dict[str, Any]] = []
    chunk_first_page = start_page
    last_page_seen = start_page - 1
    total_paginas_global: Optional[int] = None

    t0 = time.time()
    pages_done = 0

    log("Iniciando consulta ao PNCP...")
    log(f"Parâmetros: {json.dumps(params, ensure_ascii=False)}")
    log(
        f"Página inicial: {start_page} | Página final: {end_page if end_page else '∞'} | "
        f"Tamanho da página: {PAGE_SIZE} | Checkpoint a cada {checkpoint_pages} páginas"
    )

    try:
        for page_number, total_hint, filtrados in run_query_stream(
            params, esfera_alvo, start_page, end_page, quiet=True
        ):
            # primeira vez que aparece o total
            if total_paginas_global is None and total_hint is not None:
                total_paginas_global = int(total_hint)

            # progresso + ETA
            pages_done += 1
            elapsed = time.time() - t0
            avg_per_page = (elapsed / pages_done) if pages_done > 0 else None

            remaining = None
            total_str = "?"
            if total_paginas_global is not None:
                remaining = max(total_paginas_global - page_number, 0)
                total_str = str(total_paginas_global)
            elif end_page is not None:
                remaining = max(end_page - page_number, 0)
                total_str = str(end_page)
            eta_str = fmt_eta(remaining * avg_per_page) if (remaining is not None and avg_per_page is not None) else "?"

            log(f"[{page_number}/{total_str}] página processada | ETA {eta_str}")

            last_page_seen = page_number
            total_registros += len(filtrados)
            buffer_raw.extend(filtrados)

            # checkpoint a cada N páginas
            pages_in_chunk = last_page_seen - chunk_first_page + 1
            if pages_in_chunk % checkpoint_pages == 0:
                log(
                    f"[checkpoint] Salvando páginas {chunk_first_page}-{last_page_seen} "
                    f"({len(buffer_raw)} registros filtrados)..."
                )
                outputs = save_chunk(
                    buffer_raw, base, chunk_first_page, last_page_seen, only_us, only_br
                )
                outputs_all.extend(outputs)
                buffer_raw.clear()
                chunk_first_page = last_page_seen + 1

        # salva o último bloco
        if buffer_raw:
            log(
                f"[checkpoint] Salvando páginas {chunk_first_page}-{last_page_seen} "
                f"({len(buffer_raw)} registros filtrados)..."
            )
            outputs = save_chunk(
                buffer_raw, base, chunk_first_page, last_page_seen, only_us, only_br
            )
            outputs_all.extend(outputs)
            buffer_raw.clear()

    except Exception as e:
        return {
            "ok": False,
            "error": repr(e),
            "total_registros": total_registros,
            "outputs": outputs_all,
        }

    elapsed = time.time() - t0
    log(f"Tempo total: {elapsed:.1f}s")
    log(f"Total de registros (encerradas) exportados: {total_registros}")

    if outputs_all:
        log("Arquivos gerados:")
        for p in outputs_all:
            log(f" - {p}")
    else:
        log("Nenhum arquivo gerado (nenhum registro encontrado para os filtros).")

    return {
        "ok": True,
        "total_registros": total_registros,
        "outputs": outputs_all,
    }

# ---------------------------- GUI Tkinter ----------------------------

class PNCPGui(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PNCP – Licitações Encerradas (GUI)")
        self.geometry("900x650")

        self._build_widgets()
        self.worker_thread: Optional[threading.Thread] = None

    # ---------- infra de log thread-safe ----------

    def log(self, msg: str):
        def _append():
            self.txt_log.insert(tk.END, msg + "\n")
            self.txt_log.see(tk.END)
        self.txt_log.after(0, _append)

    # ---------- criação dos widgets ----------

    def _build_widgets(self):
        frm_top = ttk.Frame(self)
        frm_top.pack(fill=tk.X, padx=10, pady=5)

        # Período
        lbl_periodo = ttk.LabelFrame(frm_top, text="Período")
        lbl_periodo.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        ttk.Label(lbl_periodo, text="Ano (AAAA):").grid(row=0, column=0, sticky="w")
        self.var_ano = tk.StringVar()
        ttk.Entry(lbl_periodo, textvariable=self.var_ano, width=8).grid(row=0, column=1, sticky="w", padx=5)

        ttk.Label(lbl_periodo, text="Início (YYYY-MM-DD):").grid(row=1, column=0, sticky="w")
        self.var_inicio = tk.StringVar()
        ttk.Entry(lbl_periodo, textvariable=self.var_inicio, width=12).grid(row=1, column=1, sticky="w", padx=5)

        ttk.Label(lbl_periodo, text="Fim (YYYY-MM-DD):").grid(row=2, column=0, sticky="w")
        self.var_fim = tk.StringVar()
        ttk.Entry(lbl_periodo, textvariable=self.var_fim, width=12).grid(row=2, column=1, sticky="w", padx=5)

        ttk.Label(lbl_periodo, text="(use Ano OU Início+Fim)").grid(row=3, column=0, columnspan=2, sticky="w")

        # Filtros
        lbl_filtros = ttk.LabelFrame(frm_top, text="Filtros")
        lbl_filtros.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        ttk.Label(lbl_filtros, text="Modalidade:").grid(row=0, column=0, sticky="w")
        self.var_modalidade = tk.StringVar()
        modalidades_list = [f"{k} - {v}" for k, v in MODALIDADES.items()]
        self.cmb_modalidade = ttk.Combobox(
            lbl_filtros, textvariable=self.var_modalidade, values=modalidades_list, state="readonly", width=30
        )
        self.cmb_modalidade.grid(row=0, column=1, sticky="w", padx=5)
        if modalidades_list:
            self.cmb_modalidade.current(0)

        ttk.Label(lbl_filtros, text="Abrangência:").grid(row=1, column=0, sticky="w")
        self.var_abrangencia = tk.StringVar()
        self.cmb_abrangencia = ttk.Combobox(
            lbl_filtros, textvariable=self.var_abrangencia,
            values=ABRANGENCIAS, state="readonly", width=15
        )
        self.cmb_abrangencia.grid(row=1, column=1, sticky="w", padx=5)
        self.cmb_abrangencia.current(0)

        ttk.Label(lbl_filtros, text="UF (SP, DF ou BR):").grid(row=2, column=0, sticky="w")
        self.var_uf = tk.StringVar()
        ttk.Entry(lbl_filtros, textvariable=self.var_uf, width=6).grid(row=2, column=1, sticky="w", padx=5)

        ttk.Label(lbl_filtros, text="IBGE (7 dígitos, opcional):").grid(row=3, column=0, sticky="w")
        self.var_ibge = tk.StringVar()
        ttk.Entry(lbl_filtros, textvariable=self.var_ibge, width=10).grid(row=3, column=1, sticky="w", padx=5)

        # Paginação / saída
        frm_mid = ttk.Frame(self)
        frm_mid.pack(fill=tk.X, padx=10, pady=5)

        lbl_pag = ttk.LabelFrame(frm_mid, text="Paginação")
        lbl_pag.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        ttk.Label(lbl_pag, text="Página inicial:").grid(row=0, column=0, sticky="w")
        self.var_start_page = tk.StringVar(value="1")
        ttk.Entry(lbl_pag, textvariable=self.var_start_page, width=6).grid(row=0, column=1, sticky="w", padx=5)

        ttk.Label(lbl_pag, text="Página final (vazio = até o fim):").grid(row=1, column=0, sticky="w")
        self.var_end_page = tk.StringVar()
        ttk.Entry(lbl_pag, textvariable=self.var_end_page, width=6).grid(row=1, column=1, sticky="w", padx=5)

        ttk.Label(lbl_pag, text="Checkpoint (págs):").grid(row=2, column=0, sticky="w")
        self.var_checkpoint = tk.StringVar(value=str(DEFAULT_CHECKPOINT_PAGES))
        ttk.Entry(lbl_pag, textvariable=self.var_checkpoint, width=6).grid(row=2, column=1, sticky="w", padx=5)

        lbl_saida = ttk.LabelFrame(frm_mid, text="Saída")
        lbl_saida.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        ttk.Label(lbl_saida, text="Base do nome do arquivo:").grid(row=0, column=0, sticky="w")
        self.var_outfile = tk.StringVar()
        ttk.Entry(lbl_saida, textvariable=self.var_outfile, width=30).grid(row=0, column=1, sticky="w", padx=5)

        self.var_only_us = tk.BooleanVar(value=False)
        self.var_only_br = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            lbl_saida,
            text="Somente CSV (ponto/vírgula padrão)",
            variable=self.var_only_us
        ).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(
            lbl_saida,
            text="Somente CSV_BR (ponto-e-vírgula / vírgula decimal)",
            variable=self.var_only_br
        ).grid(row=2, column=0, columnspan=2, sticky="w")

        # Botões
        frm_buttons = ttk.Frame(self)
        frm_buttons.pack(fill=tk.X, padx=10, pady=5)

        self.btn_run = ttk.Button(frm_buttons, text="Iniciar", command=self.on_run_clicked)
        self.btn_run.pack(side=tk.LEFT, padx=5)

        self.lbl_status = ttk.Label(frm_buttons, text="Pronto.")
        self.lbl_status.pack(side=tk.LEFT, padx=10)

        # Log
        frm_log = ttk.Frame(self)
        frm_log.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        ttk.Label(frm_log, text="Log de execução:").pack(anchor="w")

        self.txt_log = tk.Text(frm_log, height=20)
        self.txt_log.pack(fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(frm_log, command=self.txt_log.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt_log.config(yscrollcommand=scrollbar.set)

    # ---------- validação e preparação ----------

    def build_params_from_ui(self):
        """
        Monta o dicionário de parâmetros da API e demais controles.
        Lança ValueError em caso de problema.
        """
        ano_str = self.var_ano.get().strip()
        inicio_str = self.var_inicio.get().strip()
        fim_str = self.var_fim.get().strip()

        if ano_str and (inicio_str or fim_str):
            raise ValueError("Use apenas o Ano OU as datas Início/Fim, não ambos.")

        if ano_str:
            if not (len(ano_str) == 4 and ano_str.isdigit()):
                raise ValueError("Ano inválido (use AAAA).")
            ano = int(ano_str)
            if not (1900 <= ano <= 2100):
                raise ValueError("Ano fora do intervalo permitido.")
            di = datetime(ano, 1, 1)
            df = datetime(ano, 12, 31)
        else:
            if not (inicio_str and fim_str):
                raise ValueError("Informe o Ano OU as duas datas Início e Fim.")
            try:
                di = datetime.strptime(inicio_str, "%Y-%m-%d")
            except Exception:
                raise ValueError("Data de início inválida (use YYYY-MM-DD).")
            try:
                df = datetime.strptime(fim_str, "%Y-%m-%d")
            except Exception:
                raise ValueError("Data de fim inválida (use YYYY-MM-DD).")
            if df < di:
                raise ValueError("Data final não pode ser anterior à inicial.")

        # modalidade
        mod_sel = self.var_modalidade.get()
        if not mod_sel:
            raise ValueError("Selecione uma modalidade.")
        try:
            mod_code = int(mod_sel.split("-", 1)[0].strip())
        except Exception:
            raise ValueError("Modalidade inválida.")
        if mod_code not in MODALIDADES:
            raise ValueError("Modalidade inexistente.")

        # abrangência
        abrang = self.var_abrangencia.get().strip().lower()
        if abrang not in ABRANGENCIAS:
            raise ValueError("Abrangência inválida.")

        params = {
            "dataInicial": di.strftime("%Y%m%d"),
            "dataFinal":   df.strftime("%Y%m%d"),
            "codigoModalidadeContratacao": mod_code,
        }

        esfera_alvo = None
        uf = self.var_uf.get().strip().upper()
        ibge = self.var_ibge.get().strip()

        if abrang == "municipal":
            if uf and uf != "BR":
                if len(uf) != 2 or not uf.isalpha():
                    raise ValueError("UF inválida.")
                params["uf"] = uf
            if ibge:
                if not (ibge.isdigit() and len(ibge) == 7):
                    raise ValueError("Código IBGE inválido (use 7 dígitos).")
                params["codigoMunicipioIbge"] = ibge
            esfera_alvo = "M"
        elif abrang == "estadual":
            if uf and uf != "BR":
                if len(uf) != 2 or not uf.isalpha():
                    raise ValueError("UF inválida.")
                params["uf"] = uf
            esfera_alvo = "E"
        elif abrang == "federal":
            esfera_alvo = "F"
        elif abrang == "distrital":
            params["uf"] = "DF"
            esfera_alvo = "D"

        # paginação
        start_page_str = self.var_start_page.get().strip() or "1"
        if not start_page_str.isdigit() or int(start_page_str) < 1:
            raise ValueError("Página inicial inválida (use inteiro >= 1).")
        start_page = int(start_page_str)

        end_page_str = self.var_end_page.get().strip()
        end_page = None
        if end_page_str:
            if not end_page_str.isdigit() or int(end_page_str) < 1:
                raise ValueError("Página final inválida (use inteiro >= 1).")
            end_page = int(end_page_str)

        checkpoint_str = self.var_checkpoint.get().strip()
        if not checkpoint_str:
            checkpoint_pages = DEFAULT_CHECKPOINT_PAGES
        else:
            if not checkpoint_str.isdigit() or int(checkpoint_str) < 1:
                raise ValueError("Checkpoint inválido (use inteiro >= 1).")
            checkpoint_pages = int(checkpoint_str)

        outfile_base = self.var_outfile.get().strip()
        if not outfile_base:
            outfile_base = f"pncp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        only_us = self.var_only_us.get()
        only_br = self.var_only_br.get()

        return params, esfera_alvo, start_page, end_page, checkpoint_pages, outfile_base, only_us, only_br

    # ---------- ações ----------

    def on_run_clicked(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Em execução", "Já existe uma consulta em andamento.")
            return

        try:
            cfg = self.build_params_from_ui()
        except ValueError as e:
            messagebox.showerror("Erro de validação", str(e))
            return

        (params, esfera_alvo, start_page, end_page,
         checkpoint_pages, base, only_us, only_br) = cfg

        self.txt_log.delete("1.0", tk.END)
        self.lbl_status.config(text="Executando...")
        self.btn_run.config(state=tk.DISABLED)

        def worker():
            result = run_pncp_job(
                params=params,
                esfera_alvo=esfera_alvo,
                start_page=start_page,
                end_page=end_page,
                checkpoint_pages=checkpoint_pages,
                base=base,
                only_us=only_us,
                only_br=only_br,
                log=self.log,
            )

            def finish():
                self.btn_run.config(state=tk.NORMAL)
                if result.get("ok"):
                    self.lbl_status.config(
                        text=f"Concluído. Registros: {result.get('total_registros', 0)}."
                    )
                    outputs = result.get("outputs") or []
                    if outputs:
                        msg = "Arquivos gerados:\n" + "\n".join(outputs)
                        messagebox.showinfo("Concluído", msg)
                    else:
                        messagebox.showinfo(
                            "Concluído", "Execução concluída. Nenhum arquivo gerado."
                        )
                else:
                    self.lbl_status.config(text="Erro na execução.")
                    messagebox.showerror(
                        "Erro na execução",
                        f"Ocorreu um erro durante a execução:\n{result.get('error')}",
                    )

            self.after(0, finish)

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()


if __name__ == "__main__":
    app = PNCPGui()
    app.mainloop()