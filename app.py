import html
import threading
import time
from datetime import datetime

import altair as alt
import numpy as np
import pandas as pd
import requests
import streamlit as st

BASE = "https://resultados.tse.jus.br"
UFS = ["ac","al","am","ap","ba","ce","df","es","go","ma","mg","ms","mt","pa","pb",
       "pe","pi","pr","rj","rn","ro","rr","rs","sc","se","sp","to","zz"]
NIVEL_PADRAO = {"1": "Brasil", "11": "Município", "13": "Município"}  # demais: UF
MAX_RPS = 20        # bem abaixo do limite de 100 req/s do TSE
MAX_PONTOS = 1000   # pontos de histórico guardados por resultado
TOP_HIST = 10       # candidatos guardados em cada ponto do histórico

st.set_page_config(page_title="Apuração TSE 2026", page_icon="🗳️", layout="wide")

# ---------------- CSS responsivo (celular) ----------------
st.markdown("""
<style>
@media (max-width: 640px) {
    h1 { font-size: 1.5rem !important; }
    h3 { font-size: 1.05rem !important; }
    h4 { font-size: 1rem !important; }

    .block-container { padding: 1rem 0.75rem 3rem !important; }

    [class*="st-key-totais"] [data-testid="stHorizontalBlock"],
    [class*="st-key-top3"] [data-testid="stHorizontalBlock"] {
        flex-wrap: wrap !important; gap: 0.5rem !important;
    }
    [class*="st-key-totais"] [data-testid="stColumn"],
    [class*="st-key-top3"] [data-testid="stColumn"] {
        min-width: calc(50% - 0.5rem) !important;
        flex: 1 1 calc(50% - 0.5rem) !important;
    }

    [data-testid="stMetricValue"] { font-size: 1.2rem !important; }
    [class*="st-key-top3"] img { width: 70px !important; }
    button[data-baseweb="tab"] p { font-size: 0.85rem !important; }
}
</style>
""", unsafe_allow_html=True)


# ---------------- HTTP (rate limit + retry) ----------------
class RateLimiter:
    """Token bucket simples e thread-safe (compartilhado entre todas as sessões)."""
    def __init__(self, rps):
        self.intervalo = 1.0 / rps
        self.lock = threading.Lock()
        self.proximo = 0.0

    def esperar(self):
        with self.lock:
            agora = time.monotonic()
            espera = self.proximo - agora
            self.proximo = max(agora, self.proximo) + self.intervalo
        if espera > 0:
            time.sleep(espera)


@st.cache_resource
def http():
    s = requests.Session()
    s.headers["User-Agent"] = "apuracao-streamlit/1.0"
    return s


@st.cache_resource
def limiter():
    return RateLimiter(MAX_RPS)


def _get(url, tentativas=3):
    r = None
    for i in range(tentativas):
        limiter().esperar()
        r = http().get(url, timeout=15)
        if r.status_code in (429, 503):
            # respeita Retry-After se vier; senão backoff exponencial: 1s, 2s, 4s
            try:
                espera = float(r.headers.get("Retry-After", 2 ** i))
            except ValueError:
                espera = 2 ** i
            time.sleep(min(espera, 10))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


@st.cache_data(ttl=300, show_spinner=False)
def get_config(url):          # arquivos de configuração mudam pouco
    return _get(url)


@st.cache_data(ttl=10, show_spinner=False)
def get_resultado(url):       # resultado: cache curto e compartilhado entre usuários
    return _get(url)


# ---------------- Histórico em memória (compartilhado) ----------------
@st.cache_resource
def store():
    return {"lock": threading.Lock(), "hist": {}, "demo_inicio": {}}


def registrar(chave, marca, pst, df):
    """Guarda um ponto (pst, % por candidato) só quando a totalização muda."""
    top = df.head(TOP_HIST)
    snap = {
        "marca": marca,
        "pst": pst,
        "hora": datetime.now().strftime("%H:%M:%S"),
        "cands": {f'{r["Candidato"]} ({r["Partido"]})': r["%"] for _, r in top.iterrows()},
    }
    s = store()
    with s["lock"]:
        h = s["hist"].setdefault(chave, [])
        if not h or h[-1]["marca"] != marca:
            h.append(snap)
            del h[:-MAX_PONTOS]
        return list(h)


def simular(df, url, intervalo):
    """Modo demo: apuração avançando 5% por atualização, convergindo ao resultado real."""
    s = store()
    with s["lock"]:
        inicio = s["demo_inicio"].setdefault(url, time.time())
    passo = int((time.time() - inicio) // intervalo) + 1
    p = min(100.0, passo * 5.0)

    rng = np.random.default_rng(len(url) * 7919)          # viés fixo por candidato
    vies = rng.normal(0, 1, len(df))
    base = (df["%"].to_numpy() * (1 + 0.25 * vies * (1 - p / 100))).clip(min=0)
    pct = base / base.sum() * 100 if base.sum() else base

    out = df.copy()
    out["%"] = pct.round(2)
    out["Votos"] = (pct / 100 * df["Votos"].sum() * p / 100).astype(int)
    out = out.sort_values("Votos", ascending=False).reset_index(drop=True)
    return out, p, f"demo-{min(passo, 20)}"


# ---------------- Helpers ----------------
def num(x) -> float:
    """TSE manda números como string e decimal com vírgula: '29,48'."""
    if x in (None, ""):
        return 0.0
    return float(str(x).replace(",", "."))


def fmt_int(x) -> str:
    return f"{int(num(x)):,}".replace(",", ".")


def montar_dir(cfg, tp, ambiente, eleicao, uf="br", pleito=""):
    """Usa os templates do atributo 'arq' da configuração de eleições (EA11)."""
    tmpl = next((a["dir"] for a in cfg.get("arq", []) if a["tp"] == tp),
                "<base>/<ambiente>/<ciclo>/<cd_eleicao>/dados/<uf>")
    return (tmpl.replace("<base>", BASE).replace("<ambiente>", ambiente)
                .replace("<ciclo>", cfg["c"]).replace("<cd_eleicao>", eleicao)
                .replace("<uf>", uf).replace("<cd_pleito>", pleito))


def url_resultado(cfg, ambiente, cd_eleicao, cd_cargo, uf="br", cod_mun=None):
    prefixo = f"{uf}{cod_mun}" if cod_mun else uf
    arquivo = f"{prefixo}-c{cd_cargo.zfill(4)}-e{cd_eleicao.zfill(6)}-u.json"
    return f'{montar_dir(cfg, "u", ambiente, cd_eleicao, uf)}/{arquivo}'


def listar_eleicoes(cfg):
    out = []
    for pl in cfg.get("pl", []):
        for e in pl.get("e", []):
            e = dict(e, pleito=pl["cd"], data=pl["dt"])
            e["label"] = f'{html.unescape(e["nm"])} — {pl["dt"]} (cód. {e["cd"]})'
            out.append(e)
    return out


def cargos_da_eleicao(ele):
    cargos = {}
    for abr in ele.get("abr", []):
        for cp in abr.get("cp", []):
            cargos[cp["cd"]] = html.unescape(cp["ds"])
    return cargos


def eleicao_presidente(eleicoes):
    """Eleição mais recente com cargo Presidente (pega o 2º turno quando publicado)."""
    cands = [e for e in eleicoes if "1" in cargos_da_eleicao(e)]
    if not cands:
        return None
    return max(cands, key=lambda e: datetime.strptime(e["data"], "%d/%m/%Y"))


def municipios(cfg_mun, uf):
    for abr in cfg_mun.get("abr", []):
        if str(abr.get("cd", "")).lower() == uf:
            return {m["cd"]: m.get("nm", m["cd"]) for m in abr.get("mu", [])}
    return {}


def candidatos(res) -> pd.DataFrame:
    rows = []
    for cargo in res.get("carg", []):
        for agr in cargo.get("agr", []):
            for par in agr.get("par", []):
                for c in par.get("cand", []):
                    rows.append({
                        "Nº": c.get("n"),
                        "Candidato": c.get("nmu") or c.get("nm"),
                        "Partido": par.get("sg"),
                        "Coligação/Federação": agr.get("nm") if agr.get("tp") in ("c", "f") else "",
                        "Votos": int(num(c.get("vap"))),
                        "%": num(c.get("pvapn") or c.get("pvap")),
                        "Situação": c.get("st"),
                        "Destinação": c.get("dvt"),
                        "sqcand": c.get("sqcand"),
                    })
    df = pd.DataFrame(rows)
    return df.sort_values("Votos", ascending=False).reset_index(drop=True) if not df.empty else df


# ---------------- Gráfico de evolução ----------------
def grafico_evolucao(hist, top=5, linha_50=False):
    ultimos = sorted(hist[-1]["cands"].items(), key=lambda kv: -kv[1])[:top]
    nomes = [n for n, _ in ultimos]
    rows = [
        {"Seções totalizadas (%)": h["pst"], "Hora": h["hora"], "Candidato": n, "% dos votos": p}
        for h in hist for n, p in h["cands"].items() if n in nomes
    ]
    dados = pd.DataFrame(rows)

    chart = alt.Chart(dados).mark_line(point=True, strokeWidth=3).encode(
        x=alt.X("Seções totalizadas (%):Q", scale=alt.Scale(domain=[0, 100])),
        y=alt.Y("% dos votos:Q"),
        color=alt.Color("Candidato:N", sort=nomes,
                        legend=alt.Legend(orient="bottom", labelLimit=140, columns=2)),
        tooltip=["Candidato", alt.Tooltip("% dos votos:Q", format=".2f"),
                 alt.Tooltip("Seções totalizadas (%):Q", format=".2f"), "Hora"],
    )
    if linha_50:
        regra = alt.Chart(pd.DataFrame({"y": [50]})).mark_rule(
            strokeDash=[6, 4], color="gray").encode(y="y:Q")
        chart = chart + regra

    st.altair_chart(chart.properties(height=320), use_container_width=True)
    if len(hist) == 1:
        st.caption("O gráfico ganha forma conforme novas totalizações são publicadas.")


# ---------------- Sidebar ----------------
st.sidebar.title("🗳️ Apuração TSE")
ambiente = st.sidebar.text_input(
    "Ambiente", "oficial",
    help="'oficial' ou o nome do ambiente de teste/simulado divulgado pelo TSE")

try:
    cfg = get_config(f"{BASE}/{ambiente}/comum/config/ele-c.json")
except Exception as ex:
    st.error(f"Não consegui ler a configuração de eleições: {ex}")
    st.stop()

st.sidebar.caption(f"Ciclo **{cfg['c']}** · config gerada em {cfg.get('dg')} {cfg.get('hg')}")

intervalo = st.sidebar.slider("Atualizar a cada (s)", 15, 120, 30, step=5)
demo = st.sidebar.toggle("Modo demonstração", help="Simula a apuração avançando, para testar o gráfico")
if st.sidebar.button("Limpar histórico"):
    s = store()
    with s["lock"]:
        s["hist"].clear()
        s["demo_inicio"].clear()

st.sidebar.divider()
st.sidebar.subheader("🔎 Explorar")

eleicoes = listar_eleicoes(cfg)
if not eleicoes:
    st.warning("Nenhuma eleição na configuração.")
    st.stop()

ele = st.sidebar.selectbox("Eleição", eleicoes, format_func=lambda e: e["label"])
cargos = cargos_da_eleicao(ele)
cd_cargo = st.sidebar.selectbox("Cargo", list(cargos), format_func=lambda c: cargos[c])

niveis = ["Brasil", "UF", "Município"]
nivel = st.sidebar.radio("Abrangência", niveis,
                         index=niveis.index(NIVEL_PADRAO.get(cd_cargo, "UF")),
                         horizontal=True)

uf, cod_mun = "br", None
if nivel != "Brasil":
    ufs_ele = [a["cd"].lower() for a in ele.get("abr", []) if a["cd"].lower() != "br"] or UFS
    uf = st.sidebar.selectbox("UF", ufs_ele, format_func=str.upper)

if nivel == "Município":
    mun_url = f'{montar_dir(cfg, "cm", ambiente, ele["cd"])}/mun-e{ele["cd"].zfill(6)}-cm.json'
    try:
        mapa = municipios(get_config(mun_url), uf)
    except Exception:
        mapa = {}
    if mapa:
        cod_mun = st.sidebar.selectbox("Município", sorted(mapa, key=mapa.get), format_func=mapa.get)
    else:
        cod_mun = st.sidebar.text_input("Código TSE do município (5 dígitos)", "").zfill(5)

url_exp = url_resultado(cfg, ambiente, ele["cd"], cd_cargo, uf, cod_mun)
foto_exp = f'{BASE}/{ambiente}/{cfg["c"]}/{ele["cd"]}/fotos/{uf}'
local_exp = {"Brasil": "Brasil", "UF": uf.upper()}.get(nivel, f"{uf.upper()} · {cod_mun}")
st.sidebar.code(url_exp, language=None)


# ---------------- Painel (auto-refresh) ----------------
@st.fragment(run_every=f"{intervalo}s")
def painel(url, titulo, local, foto_base, intervalo, demo, linha_50=False, ctx="exp"):
    try:
        res = get_resultado(url)
    except requests.HTTPError as ex:
        if ex.response is not None and ex.response.status_code in (403, 404):
            st.info("Arquivo ainda não publicado pelo TSE. Tentando de novo automaticamente…")
        else:
            st.error(f"Erro HTTP: {ex}")
        return
    except Exception as ex:
        st.error(f"Erro: {ex}")
        return

    df = candidatos(res)
    if df.empty:
        st.warning("Sem candidatos no arquivo.")
        return

    s, e, v = res.get("s", {}), res.get("e", {}), res.get("v", {})
    pst = num(s.get("pstn") or s.get("pst"))
    marca, chave = f"{res.get('dt')} {res.get('ht')}", url
    if demo:
        df, pst, marca = simular(df, url, intervalo)
        chave = "demo:" + url

    hist = registrar(chave, marca, pst, df)

    # Cabeçalho
    st.title(f"{titulo} — {local}")
    status = "🧪 simulação" if demo else (
        "✅ totalização final" if res.get("tf") == "s" and pst >= 100 else "⏳ em andamento")
    st.caption(f"Totalização: {res.get('dt')} {res.get('ht')} · {status} · "
               f"atualizado às {datetime.now():%H:%M:%S}")
    st.progress(min(pst / 100, 1.0), text=f"Seções totalizadas: {pst:.2f}%")

    # Top 3 com foto
    with st.container(key=f"top3_{ctx}"):
        cols = st.columns(3)
        for col, (_, c) in zip(cols, df.head(3).iterrows()):
            with col:
                st.image(f"{foto_base}/{c['sqcand']}.jpeg", width=110)
                st.subheader(f"{c['Candidato']} ({c['Partido']})")
                st.metric("Votos", fmt_int(c["Votos"]), f"{c['%']:.2f}%", delta_color="off")

    # Evolução em tempo real
    st.markdown("#### 📈 Evolução da apuração")
    grafico_evolucao(hist, linha_50=linha_50)

    # Totais
    with st.container(key=f"totais_{ctx}"):
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Comparecimento", f"{num(e.get('pc')):.2f}%", fmt_int(e.get("c")), delta_color="off")
        c2.metric("Abstenção", f"{num(e.get('pa')):.2f}%", fmt_int(e.get("a")), delta_color="off")
        c3.metric("Votos válidos", fmt_int(v.get("vv")))
        c4.metric("Brancos", f"{num(v.get('pvb')):.2f}%")
        c5.metric("Nulos", f"{num(v.get('ptvn')):.2f}%")

    # Ranking
    st.bar_chart(df.head(10).set_index("Candidato")["Votos"], horizontal=True)
    st.dataframe(
        df.drop(columns="sqcand"),
        hide_index=True,
        use_container_width=True,
        column_order=["Candidato", "%", "Votos", "Partido", "Situação", "Nº",
                      "Coligação/Federação", "Destinação"],
        column_config={
            "%": st.column_config.ProgressColumn("%", format="%.2f%%", min_value=0, max_value=100)
        },
    )


# ---------------- Abas ----------------
aba_pres, aba_exp = st.tabs(["🇧🇷 Presidente ao vivo", "🔎 Explorar resultados"])

with aba_pres:
    pres = eleicao_presidente(eleicoes)
    if pres:
        painel(
            url_resultado(cfg, ambiente, pres["cd"], "1"),
            f"Presidente · {pres['t']}º turno", "Brasil",
            f'{BASE}/{ambiente}/{cfg["c"]}/{pres["cd"]}/fotos/br',
            intervalo, demo, linha_50=True, ctx="pres",
        )
    else:
        st.info(f"A eleição presidencial ainda não aparece na configuração do TSE "
                f"(ciclo atual: **{cfg['c']}**). Até ser publicada, esta aba mostra "
                f"a seleção da barra lateral para teste.")
        painel(url_exp, cargos.get(cd_cargo, ""), local_exp, foto_exp,
               intervalo, demo, linha_50=True, ctx="pres")

with aba_exp:
    painel(url_exp, cargos.get(cd_cargo, ""), local_exp, foto_exp, intervalo, demo, ctx="exp")