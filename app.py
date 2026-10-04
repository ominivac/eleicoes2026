import html
import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import altair as alt
import numpy as np
import pandas as pd
import requests
import streamlit as st

AUTOR = "Roberto Sousa"
EMAIL = "ominivac001@proton.me"   # <-- coloque seu e-mail aqui

TZ = ZoneInfo("America/Sao_Paulo")
FONTES = {
    "Oficial (dia da eleição)": ("https://resultados.tse.jus.br", "oficial"),
    "Simulado TSE 2026": ("https://resultados-sim.tse.jus.br/simulado", "simulado2026"),
}
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
    [class*="st-key-top3"] [data-testid="stHorizontalBlock"],
    [class*="st-key-dif"] [data-testid="stHorizontalBlock"] {
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


# ---------------- Secrets ----------------
def segredo(nome):
    try:
        return st.secrets[nome]
    except Exception:
        return None


def ler_sb():
    url, key = segredo("SUPABASE_URL"), segredo("SUPABASE_KEY")
    return {"url": url, "key": key} if url and key else None


def sb_headers(sb):
    return {"apikey": sb["key"], "Authorization": f'Bearer {sb["key"]}',
            "Content-Type": "application/json"}


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
def get_config(url):
    return _get(url)


@st.cache_data(ttl=10, show_spinner=False)
def get_resultado(url):
    return _get(url)


# ---------------- Histórico (memória + Supabase) ----------------
@st.cache_resource
def store():
    return {"lock": threading.Lock(), "hist": {}, "demo_inicio": {},
            "carregado": set(), "sb": ler_sb(), "erro_sb": None}


def sb_carregar(s, chave):
    """Busca no Supabase os pontos já gravados (sobrevive a reinícios do app)."""
    sb = s["sb"]
    if not sb or chave.startswith("demo:"):
        return []
    try:
        r = requests.get(
            f'{sb["url"]}/rest/v1/historico',
            params={"chave": f"eq.{chave}", "select": "marca,pst,hora,cands",
                    "order": "criado_em.desc", "limit": str(MAX_PONTOS)},
            headers=sb_headers(sb), timeout=10,
        )
        r.raise_for_status()
        return list(reversed(r.json()))
    except Exception as ex:
        s["erro_sb"] = f"carregar: {str(ex)[:120]}"
        return []


def sb_salvar(s, chave, snap):
    sb = s["sb"]
    if not sb or chave.startswith("demo:"):
        return
    try:
        r = requests.post(
            f'{sb["url"]}/rest/v1/historico',
            params={"on_conflict": "chave,marca"},
            headers={**sb_headers(sb), "Prefer": "resolution=ignore-duplicates,return=minimal"},
            json={"chave": chave, **snap}, timeout=5,
        )
        r.raise_for_status()
        s["erro_sb"] = None
    except Exception as ex:
        s["erro_sb"] = f"salvar: {str(ex)[:120]}"


def garantir_carregado(s, chave):
    """Na primeira vez que uma chave aparece após o app subir, recarrega do banco."""
    with s["lock"]:
        if chave in s["carregado"]:
            return
        s["carregado"].add(chave)
    pontos = sb_carregar(s, chave)
    if pontos:
        with s["lock"]:
            h = s["hist"].setdefault(chave, [])
            existentes = {p["marca"] for p in h}
            h[:0] = [p for p in pontos if p["marca"] not in existentes]
            del h[:-MAX_PONTOS]


def registrar(chave, marca, pst, df, s=None):
    """Guarda um ponto (pst, % por candidato) só quando a totalização muda."""
    s = s or store()
    garantir_carregado(s, chave)
    top = df.head(TOP_HIST)
    snap = {
        "marca": marca,
        "pst": float(pst),
        "hora": datetime.now(TZ).strftime("%H:%M:%S"),
        "cands": {f'{r["Candidato"]} ({r["Partido"]})': float(r["%"]) for _, r in top.iterrows()},
    }
    novo = False
    with s["lock"]:
        h = s["hist"].setdefault(chave, [])
        if not any(p["marca"] == marca for p in h[-5:]):
            h.append(snap)
            del h[:-MAX_PONTOS]
            novo = True
        out = list(h)
    if novo:
        sb_salvar(s, chave, snap)
    return out


def limpar_historico():
    """Apaga memória e banco (só para o administrador)."""
    s = store()
    with s["lock"]:
        s["carregado"].update(s["hist"].keys())   # não recarrega o que acabou de apagar
        s["hist"].clear()
        s["demo_inicio"].clear()
    sb = s["sb"]
    if sb:
        try:
            requests.delete(f'{sb["url"]}/rest/v1/historico',
                            params={"chave": "neq.__nenhuma__"},
                            headers=sb_headers(sb), timeout=10).raise_for_status()
        except Exception as ex:
            s["erro_sb"] = f"apagar: {str(ex)[:120]}"


def simular(df, url, intervalo):
    """Modo demo: apuração avançando 5% por atualização, convergindo ao resultado real."""
    s = store()
    with s["lock"]:
        inicio = s["demo_inicio"].setdefault(url, time.time())
    passo = int((time.time() - inicio) // intervalo) + 1
    p = min(100.0, passo * 5.0)

    rng = np.random.default_rng(len(url) * 7919)
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


def montar_dir(cfg, tp, base, ambiente, ciclo, eleicao, uf="br", pleito=""):
    """Usa os templates do atributo 'arq' da configuração de eleições (EA11)."""
    padrao = {"ft": "<base>/<ambiente>/<ciclo>/<cd_eleicao>/fotos/<uf>",
              "cm": "<base>/<ambiente>/<ciclo>/<cd_eleicao>/config"}
    tmpl = next((a["dir"] for a in cfg.get("arq", []) if a["tp"] == tp),
                padrao.get(tp, "<base>/<ambiente>/<ciclo>/<cd_eleicao>/dados/<uf>"))
    return (tmpl.replace("<base>", base).replace("<ambiente>", ambiente)
                .replace("<ciclo>", ciclo).replace("<cd_eleicao>", eleicao)
                .replace("<uf>", uf).replace("<cd_pleito>", pleito))


def url_resultado(cfg, base, ambiente, ele, cd_cargo, uf="br", cod_mun=None):
    prefixo = f"{uf}{cod_mun}" if cod_mun else uf
    arquivo = f'{prefixo}-c{cd_cargo.zfill(4)}-e{ele["cd"].zfill(6)}-u.json'
    return f'{montar_dir(cfg, "u", base, ambiente, ele["ciclo"], ele["cd"], uf)}/{arquivo}'


def listar_eleicoes(cfg):
    out = []
    for pl in cfg.get("pl", []):
        # 2026: ciclo fica dentro de cada pleito; formato antigo: no topo do arquivo
        ciclo = pl.get("c") or cfg.get("c", "")
        for e in pl.get("e", []):
            e = dict(e, pleito=pl["cd"], data=pl["dt"], ciclo=ciclo)
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
    return max(cands, key=lambda e: (datetime.strptime(e["data"], "%d/%m/%Y"), int(e.get("t", 1))))


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


# ---------------- Coletor em segundo plano ----------------
@st.cache_resource
def coletor(url, intervalo=20):
    """Thread única no servidor que grava o histórico mesmo sem ninguém no app.
    Para sozinha quando a totalização chega ao fim."""
    s, sess, lim = store(), http(), limiter()
    estado = {"ultimo_ok": None, "erro": None, "final": False}

    def loop():
        while True:
            try:
                lim.esperar()
                r = sess.get(url, timeout=15)
                r.raise_for_status()
                res = r.json()
                df = candidatos(res)
                if not df.empty:
                    sx = res.get("s", {})
                    pst = num(sx.get("pstn") or sx.get("pst"))
                    registrar(url, f"{res.get('dt')} {res.get('ht')}", pst, df, s)
                    estado["ultimo_ok"] = datetime.now(TZ).strftime("%H:%M:%S")
                    estado["erro"] = None
                    if res.get("tf") == "s" and pst >= 100:
                        estado["final"] = True
                        return
            except Exception as ex:
                estado["erro"] = str(ex)[:120]
            time.sleep(intervalo)

    threading.Thread(target=loop, daemon=True).start()
    return estado


# ---------------- Contador de visitantes ----------------
@st.cache_resource
def visitas():
    return {"lock": threading.Lock(), "total": 0, "online": {}}


def incrementar_supabase():
    """Soma +1 no banco e devolve o total. Em caso de falha, guarda o motivo."""
    sb = ler_sb()
    if not sb:
        st.session_state.erro_supabase = "secrets SUPABASE_URL/SUPABASE_KEY ausentes"
        return None
    try:
        r = http().post(f'{sb["url"]}/rest/v1/rpc/incrementar_visitas',
                        headers=sb_headers(sb), json={}, timeout=5)
        r.raise_for_status()
        total = r.json()
        if total is None:
            raise ValueError("função retornou vazio (linha id=1 não existe?)")
        st.session_state.pop("erro_supabase", None)
        return int(total)
    except requests.HTTPError as ex:
        st.session_state.erro_supabase = f"HTTP {ex.response.status_code}: {ex.response.text[:150]}"
    except Exception as ex:
        st.session_state.erro_supabase = str(ex)
    return None


def registrar_visita():
    v = visitas()
    if "sid" not in st.session_state:
        st.session_state.sid = uuid.uuid4().hex
        total_banco = incrementar_supabase()
        with v["lock"]:
            v["total"] = total_banco if total_banco is not None else v["total"] + 1
    with v["lock"]:
        v["online"][st.session_state.sid] = time.time()


def contar_visitas(janela=60):
    v = visitas()
    corte = time.time() - janela
    with v["lock"]:
        for sid, t in list(v["online"].items()):
            if t < corte:
                del v["online"][sid]
        return len(v["online"]), v["total"]


@st.fragment(run_every="20s")
def contador():
    registrar_visita()
    online, total = contar_visitas()
    c1, c2 = st.columns(2)
    c1.metric("👀 Online", online)
    c2.metric("📊 Visitas", fmt_int(total))
    if "erro_supabase" in st.session_state:
        st.caption(f"⚠️ Contador só em memória — {st.session_state.erro_supabase}")


# ---------------- Gráfico de evolução ----------------
def grafico_evolucao(hist, top=5, linha_50=False):
    try:
        if not hist:
            st.warning("Histórico vazio — nenhum ponto registrado ainda.")
            return

        ultimos = sorted(hist[-1]["cands"].items(), key=lambda kv: -kv[1])[:top]
        nomes = [n for n, _ in ultimos]
        rows = [
            {"Seções totalizadas (%)": float(h["pst"]), "Hora": h["hora"],
             "Candidato": n, "% dos votos": float(p)}
            for h in hist for n, p in h["cands"].items() if n in nomes
        ]
        dados = pd.DataFrame(rows)
        if dados.empty:
            st.warning("Histórico sem dados de candidatos.")
            return

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

    except Exception as ex:
        st.error(f"Erro ao desenhar o gráfico: {type(ex).__name__}: {ex}")

    with st.expander("🔧 Diagnóstico do gráfico"):
        st.write(f"Pontos no histórico: {len(hist)}")
        if hist:
            st.json(hist[-1])


# ---------------- Painel de diferenças ----------------
def painel_diferencas(df, votos_validos, ctx, top=5, mostrar_50=False):
    st.markdown("#### ⚖️ Diferença de votos entre os candidatos")
    d = df.head(top).reset_index(drop=True)
    if len(d) < 2:
        st.caption("Há só um candidato neste resultado.")
        return

    lider, segundo = d.iloc[0], d.iloc[1]
    dif_votos = int(lider["Votos"] - segundo["Votos"])
    dif_pp = float(lider["%"] - segundo["%"])

    with st.container(key=f"dif_{ctx}"):
        cols = st.columns(2 if mostrar_50 and votos_validos else 1)
        cols[0].metric(
            f"{lider['Candidato']} à frente de {segundo['Candidato']}",
            f"{fmt_int(dif_votos)} votos",
            f"{dif_pp:.2f} p.p.", delta_color="off",
        )
        if mostrar_50 and votos_validos:
            margem = int(lider["Votos"] - votos_validos / 2)
            if margem > 0:
                cols[1].metric(f"{lider['Candidato']} acima de 50% dos válidos",
                               f"+{fmt_int(margem)} votos", "vence no 1º turno se mantiver",
                               delta_color="normal")
            else:
                cols[1].metric(f"Faltam para {lider['Candidato']} chegar a 50%",
                               f"{fmt_int(-margem)} votos", "abaixo da maioria → 2º turno",
                               delta_color="inverse")

    atras_lider = lider["Votos"] - d["Votos"]
    atras_anterior = d["Votos"].shift(1) - d["Votos"]
    tabela = pd.DataFrame({
        "Pos.": [f"{i}º" for i in range(1, len(d) + 1)],
        "Candidato": d["Candidato"] + " (" + d["Partido"] + ")",
        "Votos": d["Votos"].map(fmt_int),
        "%": d["%"].map(lambda x: f"{x:.2f}%"),
        "Atrás do líder": ["—"] + [fmt_int(x) for x in atras_lider[1:]],
        "Atrás do anterior": ["—"] + [fmt_int(x) for x in atras_anterior[1:]],
    })
    st.dataframe(tabela, hide_index=True, use_container_width=True)


# ---------------- Sidebar ----------------
admin = bool(segredo("ADMIN_KEY")) and st.query_params.get("admin") == segredo("ADMIN_KEY")

st.sidebar.title("🗳️ Apuração TSE")
st.sidebar.caption(f"por **{AUTOR}** · {EMAIL}")
with st.sidebar:
    contador()

fonte = st.sidebar.selectbox("Fonte de dados", list(FONTES))
BASE, ambiente = FONTES[fonte]
with st.sidebar.expander("Avançado"):
    BASE = st.text_input("Host", BASE)
    ambiente = st.text_input("Ambiente", ambiente)

try:
    cfg = get_config(f"{BASE}/{ambiente}/comum/config/ele-c.json")
except Exception as ex:
    st.error(f"Não consegui ler a configuração de eleições ({fonte}): {ex}. "
             f"O TSE pode não estar publicando os arquivos desta fonte agora — "
             f"tente a outra fonte na barra lateral.")
    st.stop()

eleicoes = listar_eleicoes(cfg)
if not eleicoes:
    st.warning("Nenhuma eleição na configuração.")
    st.stop()

ciclos = sorted({e["ciclo"] for e in eleicoes if e["ciclo"]})
st.sidebar.caption(f"Ciclo **{', '.join(ciclos)}** · config gerada em {cfg.get('dg')} {cfg.get('hg')}")

intervalo = st.sidebar.slider("Atualizar a cada (s)", 15, 120, 30, step=5)
demo = st.sidebar.toggle("Modo demonstração", help="Simula a apuração avançando, para testar o gráfico")

if admin:
    st.sidebar.success("Modo administrador")
    if st.sidebar.button("🗑️ Limpar histórico (memória e banco)"):
        limpar_historico()
        st.sidebar.info("Histórico apagado.")
    erro_sb = store()["erro_sb"]
    st.sidebar.caption(f"Banco do histórico: {'⚠️ ' + erro_sb if erro_sb else '✅ ok'}"
                       if store()["sb"] else "Banco do histórico: ⚠️ sem secrets do Supabase")

st.sidebar.divider()
st.sidebar.subheader("🔎 Explorar")

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
    mun_dir = montar_dir(cfg, "cm", BASE, ambiente, ele["ciclo"], ele["cd"])
    mun_url = f'{mun_dir}/mun-e{ele["cd"].zfill(6)}-cm.json'
    try:
        mapa = municipios(get_config(mun_url), uf)
    except Exception:
        mapa = {}
    if mapa:
        cod_mun = st.sidebar.selectbox("Município", sorted(mapa, key=mapa.get), format_func=mapa.get)
    else:
        cod_mun = st.sidebar.text_input("Código TSE do município (5 dígitos)", "").zfill(5)

url_exp = url_resultado(cfg, BASE, ambiente, ele, cd_cargo, uf, cod_mun)
foto_exp = montar_dir(cfg, "ft", BASE, ambiente, ele["ciclo"], ele["cd"], uf)
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
               f"atualizado às {datetime.now(TZ):%H:%M:%S} · "
               f"{len(hist)} pontos no gráfico (último às {hist[-1]['hora']})")
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

    # Diferença de votos entre os candidatos
    vv = df["Votos"].sum() if demo else num(v.get("vv"))
    painel_diferencas(df, vv, ctx, mostrar_50=linha_50)

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
        url_pres = url_resultado(cfg, BASE, ambiente, pres, "1")

        # coletor em segundo plano: grava o histórico mesmo sem ninguém no app
        if not demo:
            estado = coletor(url_pres)
            if estado["erro"]:
                st.caption(f"⚠️ Coletor: {estado['erro']}")

        painel(
            url_pres,
            f"Presidente · {pres['t']}º turno", "Brasil",
            montar_dir(cfg, "ft", BASE, ambiente, pres["ciclo"], pres["cd"], "br"),
            intervalo, demo, linha_50=True, ctx="pres",
        )
    else:
        st.info("A eleição presidencial ainda não aparece na configuração desta fonte. "
                "Até ser publicada, esta aba mostra a seleção da barra lateral para teste.")
        painel(url_exp, cargos.get(cd_cargo, ""), local_exp, foto_exp,
               intervalo, demo, linha_50=True, ctx="pres")

with aba_exp:
    painel(url_exp, cargos.get(cd_cargo, ""), local_exp, foto_exp, intervalo, demo, ctx="exp")

st.divider()
st.caption(f"Desenvolvido por **{AUTOR}** · {EMAIL} · Dados: TSE")