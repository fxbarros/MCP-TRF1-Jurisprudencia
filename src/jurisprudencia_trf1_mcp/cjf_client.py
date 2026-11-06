"""Cliente para o portal de jurisprudência do CJF (antiga-jurisprudencia.cjf.jus.br).

O portal é um app JSF/PrimeFaces 6.2 sem Cloudflare nem captcha na busca.
Fluxo por consulta (stateless — cada busca abre sessão nova):
  1. GET  {base}/index.xhtml            -> cookie JSESSIONID + javax.faces.ViewState
  2. POST {base}/index.xhtml (form)     -> página 1 de resultados (HTML completo)
  3. POST parcial PrimeFaces (opcional) -> páginas seguintes (first=N*30)

As requisições saem por um de dois caminhos (variável de ambiente CJF_BACKEND):
  - "camoufox" (padrão): o navegador Camoufox, com a janela OCULTA, que fica
    aberto entre as consultas. CJF_HEADLESS=0 mostra a janela, que exibe a
    página de resultados da última busca (sempre a primeira página; a
    paginação é pedida por baixo, via fetch).
  - "httpx": requisições diretas, sem navegador.
O fluxo, os campos enviados e o parsing são os mesmos nos dois caminhos.

Todo o conteúdo da decisão (número, classe, relator, órgão, datas, ementa,
dispositivo) vem inline no HTML dos resultados; o "inteiro teor" do portal é
apenas um link para a consulta pública do PJe.
"""
from __future__ import annotations

import asyncio
import contextlib
import html as html_lib
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import httpx

# por onde as requisições saem: "camoufox" (padrão) ou "httpx"
BACKEND = os.environ.get("CJF_BACKEND", "camoufox").strip().lower()
# a janela do Camoufox fica oculta por padrão; CJF_HEADLESS=0 a mostra
HEADLESS = os.environ.get("CJF_HEADLESS", "1").strip() != "0"

# Em 2026 o endereço jurisprudencia.cjf.jus.br passou a servir o portal novo
# ("Jurisprudência Unificada", app Angular); o portal JSF que este cliente
# consulta continua no ar no host antiga-jurisprudencia.
BASES = {
    "trf1": "https://antiga-jurisprudencia.cjf.jus.br/trf1/index.xhtml",
    "colegiado": "https://antiga-jurisprudencia.cjf.jus.br/colegiado/index.xhtml",
}

TIPOS_VALIDOS = {"ACORDAO", "SUMULA", "ARGUICAO", "DECISAOMONO"}
FONTES_VALIDAS = {"TRF1", "JEF1"}
TIPOS_DATA = {"julgamento": "DTDP", "publicacao": "DTPP"}

# campos do painel "Pesquisa avançada" (base trf1). Os names j_idtNN vêm do
# deploy atual do portal e podem mudar num redeploy — por isso o mapeamento
# real é extraído do HTML do painel a cada consulta (_mapear_campos_avancados,
# via os ids ESTÁVEIS dos <label>), com este dicionário como fallback. O
# painel só é processado pelo JSF depois de um POST AJAX que liga o checkbox
# ckbAvancada (ver _ligar_pesquisa_avancada).
CAMPOS_AVANCADOS = {
    "numero": "formulario:j_idt28",
    "classe": "formulario:j_idt30",
    "relator": "formulario:j_idt32",
    "revisor": "formulario:j_idt34",
    "relator_convocado": "formulario:j_idt36",
    "relator_para_acordao": "formulario:j_idt38",
    "orgao_julgador": "formulario:j_idt40",
    "origem": "formulario:j_idt42",
    "ementa_decisao": "formulario:j_idt44",
    "referencia_legislativa": "formulario:j_idt46",
    "data_inicio": "formulario:j_idt48_input",
    "data_fim": "formulario:j_idt50_input",
}

# id estável do <label> no painel avançado -> nome do filtro aqui
_LABELS_AVANCADOS = {
    "proc": "numero",
    "combo_classes": "classe",
    "rel": "relator",
    "rev": "revisor",
    "relc": "relator_convocado",
    "rela": "relator_para_acordao",
    "combo_orgaos": "orgao_julgador",
    "_origem": "origem",
    "emen": "ementa_decisao",
    "refl": "referencia_legislativa",
}

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_RE_VIEWSTATE = re.compile(
    r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"'
)
# ViewState devolvido no XML das respostas parciais (partial/ajax)
_RE_VIEWSTATE_XML = re.compile(
    r'<update id="[^"]*ViewState[^"]*"><!\[CDATA\[([^\]]+)\]\]>'
)
_RE_LABEL_ID = re.compile(r'<label[^>]*\bfor="([A-Za-z_]\w*)"', re.I)
_RE_INPUT_NAME = re.compile(r'<input[^>]*\bname="(formulario:[^"]+)"', re.I)
_RE_DOC_SPLIT = re.compile(
    r'<table class="table_pesquisa_lista" id="doc_(\d+)"'
)
_RE_TAGS = re.compile(r"<[^>]+>")

# rótulos dos campos exibidos em cada item de resultado, na ordem do portal
_CAMPOS = [
    ("Tipo", "tipo"),
    ("Número", "numero"),
    ("Classe", "classe"),
    ("Relator(a)", "relator"),
    ("Relator(a) para acórdão", "relator_para_acordao"),
    ("Origem", "origem"),
    ("Órgão julgador", "orgao_julgador"),
    ("Data", "data_julgamento"),
    ("Data da publicação", "data_publicacao"),
    ("Fonte da publicação", "fonte_publicacao"),
    ("Ementa", "ementa"),
    ("Decisão", "decisao"),
]
_ROTULOS = {rotulo for rotulo, _ in _CAMPOS}
# rótulos que encerram a captura de um campo multilinha
_FIM_DE_CAMPO = _ROTULOS | {"Inteiro teor", "Acesse Aqui"}


class ConsultaFalhou(Exception):
    """O portal respondeu com erro ou HTML inesperado."""


def _normalizar(texto: str) -> str:
    """minúsculas, sem acentos, espaços colapsados — para comparação tolerante."""
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", texto).strip().lower()


def _so_digitos(numero: str) -> str:
    return re.sub(r"\D", "", numero)


def _mapear_campos_avancados(painel_html: str) -> dict[str, str]:
    """Extrai o mapeamento filtro -> name do input a partir do HTML do painel.

    Os <label> do painel têm ids estáveis (proc, rel, combo_orgaos...); o
    input de cada campo é o primeiro <input name="formulario:..."> depois do
    label. As datas não têm label próprio: são os dois calendários
    j_idtNN_input do painel, na ordem início/fim.
    """
    campos: dict[str, str] = {}
    for m in _RE_LABEL_ID.finditer(painel_html):
        chave = _LABELS_AVANCADOS.get(m.group(1))
        if not chave or chave in campos:
            continue
        m2 = _RE_INPUT_NAME.search(painel_html, m.end())
        if m2:
            campos[chave] = m2.group(1)
    calendarios = [
        n
        for n in _RE_INPUT_NAME.findall(painel_html)
        if re.fullmatch(r"formulario:j_idt\d+_input", n)
    ]
    if len(calendarios) >= 2:
        campos["data_inicio"], campos["data_fim"] = calendarios[0], calendarios[1]
    return campos


def _normalizar_data(data: str, rotulo: str) -> str:
    """Aceita DD/MM/AAAA ou AAAA-MM-DD e devolve DD/MM/AAAA (formato do portal)."""
    data = data.strip()
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", data):
        return data
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", data)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    raise ValueError(
        f"{rotulo} inválida: {data!r}; use DD/MM/AAAA ou AAAA-MM-DD"
    )


@dataclass
class Decisao:
    doc_id: str
    tipo: str = ""
    numero: str = ""
    classe: str = ""
    relator: str = ""
    relator_para_acordao: str = ""
    origem: str = ""
    orgao_julgador: str = ""
    data_julgamento: str = ""
    data_publicacao: str = ""
    fonte_publicacao: str = ""
    ementa: str = ""
    decisao: str = ""
    url_inteiro_teor: str = ""

    def to_dict(self, resumido: bool = False) -> dict:
        d = {
            "doc_id": self.doc_id,
            "tipo": self.tipo,
            "numero": self.numero,
            "classe": self.classe,
            "relator": self.relator,
            "orgao_julgador": self.orgao_julgador,
            "data_julgamento": self.data_julgamento,
            "data_publicacao": self.data_publicacao,
            "ementa": self.ementa,
        }
        if self.relator_para_acordao:
            d["relator_para_acordao"] = self.relator_para_acordao
        if not resumido:
            d.update(
                {
                    "origem": self.origem,
                    "fonte_publicacao": self.fonte_publicacao,
                    "decisao": self.decisao,
                    "url_inteiro_teor": self.url_inteiro_teor,
                }
            )
        return d

    def citacao_abnt(self) -> str:
        tribunal = (
            "Tribunal Regional Federal da 1ª Região"
            if self.origem.upper().startswith("TRF")
            else (self.origem.title() or "Conselho da Justiça Federal")
        )
        partes = [f"BRASIL. {tribunal}."]
        if self.classe or self.numero:
            partes.append(f"{self.classe or self.tipo} {self.numero}.".strip())
        if self.relator:
            partes.append(f"Relator(a): {self.relator.title()}.")
        if self.orgao_julgador:
            partes.append(f"{self.orgao_julgador.title()}.")
        if self.data_julgamento:
            partes.append(f"Julgado em {self.data_julgamento}.")
        if self.fonte_publicacao:
            partes.append(f"Publicação: {self.fonte_publicacao}.")
        return " ".join(partes)


@dataclass
class Busca:
    total_no_servidor: int
    resultados: list[Decisao] = field(default_factory=list)


# ------------------------------------------------------------------ transporte
#
# Uma "sessão" é o canal de UMA consulta ao portal e expõe três operações:
#   abrir(url)        -> GET da página inicial (HTML)
#   enviar(url, form) -> POST completo do formulário (HTML dos resultados)
#   ajax(url, form)   -> POST parcial PrimeFaces (XML partial-response)

_HEADERS_AJAX = {
    "Faces-Request": "partial/ajax",
    "X-Requested-With": "XMLHttpRequest",
}

# envia o formulário pela própria página, para a janela exibir o resultado
_JS_ENVIAR = """([url, pares]) => {
    const f = document.createElement('form');
    f.method = 'post';
    f.action = url;
    f.acceptCharset = 'UTF-8';
    f.style.display = 'none';
    for (const [nome, valor] of pares) {
        const campo = document.createElement('input');
        campo.type = 'hidden';
        campo.name = nome;
        campo.value = valor;
        f.appendChild(campo);
    }
    document.body.appendChild(f);
    HTMLFormElement.prototype.submit.call(f);
}"""

_JS_AJAX = """async ([url, pares, cabecalhos]) => {
    const corpo = new URLSearchParams();
    for (const [nome, valor] of pares) corpo.append(nome, valor);
    const r = await fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            ...cabecalhos,
        },
        body: corpo.toString(),
        credentials: 'same-origin',
    });
    return {status: r.status, texto: await r.text()};
}"""


def _pares(form: dict) -> list[list[str]]:
    """Achata o form (valores str ou lista) em pares [nome, valor], na ordem."""
    pares: list[list[str]] = []
    for nome, valor in form.items():
        for v in valor if isinstance(valor, (list, tuple)) else [valor]:
            pares.append([nome, v])
    return pares


class _SessaoHttpx:
    """Requisições diretas, sem navegador (CJF_BACKEND=httpx)."""

    def __init__(self, timeout: float):
        self._http = httpx.AsyncClient(
            headers={"User-Agent": _UA},
            timeout=timeout,
            follow_redirects=True,
        )

    async def abrir(self, url: str) -> str:
        r = await self._http.get(url)
        r.raise_for_status()
        return r.text

    async def enviar(self, url: str, form: dict) -> str:
        r = await self._http.post(url, data=form)
        r.raise_for_status()
        return r.text

    async def ajax(self, url: str, form: dict) -> str:
        r = await self._http.post(url, data=form, headers=_HEADERS_AJAX)
        r.raise_for_status()
        return r.text

    async def fechar(self) -> None:
        await self._http.aclose()


async def _html_da_resposta(resp: Any, page: Any) -> str:
    """HTML bruto de uma navegação, igual ao que o httpx receberia."""
    if resp is None:
        raise ConsultaFalhou("o navegador não recebeu resposta do portal")
    if resp.status >= 400:
        raise ConsultaFalhou(f"o portal respondeu HTTP {resp.status}")
    try:
        corpo = await resp.body()
    except Exception:
        # corpo indisponível no navegador: cai no HTML já carregado na página
        return await page.content()
    m = re.search(r"charset=([\w-]+)", resp.headers.get("content-type", ""), re.I)
    try:
        return corpo.decode(m.group(1) if m else "utf-8", errors="replace")
    except LookupError:
        return corpo.decode("utf-8", errors="replace")


class _SessaoNavegador:
    """Uma consulta feita pela janela do Camoufox."""

    def __init__(self, page: Any):
        self._page = page

    async def abrir(self, url: str) -> str:
        # cada busca abre sessão nova no portal, como no caminho direto
        await self._page.context.clear_cookies()
        resp = await self._page.goto(url, wait_until="domcontentloaded")
        return await _html_da_resposta(resp, self._page)

    async def enviar(self, url: str, form: dict) -> str:
        async with self._page.expect_navigation(
            wait_until="domcontentloaded"
        ) as navegacao:
            await self._page.evaluate(_JS_ENVIAR, [url, _pares(form)])
        return await _html_da_resposta(await navegacao.value, self._page)

    async def ajax(self, url: str, form: dict) -> str:
        r = await self._page.evaluate(
            _JS_AJAX, [url, _pares(form), _HEADERS_AJAX]
        )
        if r["status"] >= 400:
            raise ConsultaFalhou(
                f"o portal respondeu HTTP {r['status']} à requisição parcial"
            )
        return r["texto"]


class _Navegador:
    """Mantém o Camoufox vivo e serializa as consultas numa única página.

    O navegador abre na primeira consulta e fica aberto até o servidor
    encerrar; se for fechado à mão, a consulta seguinte abre outro.
    """

    def __init__(self, timeout: float):
        self._timeout_ms = timeout * 1000
        self._pilha: contextlib.AsyncExitStack | None = None
        self._browser: Any = None
        self._page: Any = None
        self.lock = asyncio.Lock()

    async def pagina(self) -> Any:
        if self._page is not None and not self._page.is_closed():
            return self._page
        if self._browser is None or not self._browser.is_connected():
            await self.fechar()
            try:
                # importado aqui para não pesar no handshake do MCP
                from camoufox.async_api import AsyncCamoufox

                pilha = contextlib.AsyncExitStack()
                # o stdout é o canal do protocolo MCP: nada do Camoufox pode
                # ser impresso nele
                with contextlib.redirect_stdout(sys.stderr):
                    self._browser = await pilha.enter_async_context(
                        AsyncCamoufox(headless=HEADLESS)
                    )
                self._pilha = pilha
            except Exception as e:
                raise ConsultaFalhou(
                    f"não foi possível abrir o navegador Camoufox "
                    f"({type(e).__name__}: {str(e)[:200]}). Rode "
                    "`uv run camoufox fetch` na pasta do projeto ou suba o "
                    "servidor com CJF_BACKEND=httpx para consultar sem navegador"
                ) from e
        self._page = await self._browser.new_page()
        self._page.set_default_timeout(self._timeout_ms)
        return self._page

    async def fechar(self) -> None:
        pilha, self._pilha = self._pilha, None
        self._browser = self._page = None
        if pilha is not None:
            with contextlib.suppress(Exception):
                await pilha.aclose()


class CJFClient:
    def __init__(self, timeout: float = 40.0):
        self._timeout = timeout
        self._navegador = _Navegador(timeout)

    @contextlib.asynccontextmanager
    async def _sessao(self):
        """Canal de uma consulta, conforme CJF_BACKEND."""
        if BACKEND == "httpx":
            sessao = _SessaoHttpx(self._timeout)
            try:
                yield sessao
            finally:
                await sessao.fechar()
        else:
            async with self._navegador.lock:
                yield _SessaoNavegador(await self._navegador.pagina())

    async def fechar(self) -> None:
        """Fecha a janela do navegador (se houver)."""
        await self._navegador.fechar()

    async def buscar(
        self,
        query: str,
        base: str = "trf1",
        tipos: list[str] | None = None,
        fontes: list[str] | None = None,
        page: int = 1,
        *,
        por_pagina: int = 30,
        tipo_data: str = "julgamento",
        **filtros_avancados: str,
    ) -> Busca:
        """Pesquisa no portal. `filtros_avancados` aceita as chaves de
        CAMPOS_AVANCADOS (relator, orgao_julgador, data_inicio, ...); qualquer
        filtro preenchido dispara o handshake da pesquisa avançada.
        """
        if base not in BASES:
            raise ValueError(f"base inválida: {base!r}; use {sorted(BASES)}")
        url = BASES[base]
        tipos = [t.upper() for t in (tipos or ["ACORDAO"])]
        invalidos = set(tipos) - TIPOS_VALIDOS
        if invalidos:
            raise ValueError(
                f"tipos inválidos: {sorted(invalidos)}; use {sorted(TIPOS_VALIDOS)}"
            )
        fontes = [f.upper() for f in (fontes or ["TRF1", "JEF1"])]
        invalidas = set(fontes) - FONTES_VALIDAS
        if invalidas:
            raise ValueError(
                f"fontes inválidas: {sorted(invalidas)}; use {sorted(FONTES_VALIDAS)}"
            )
        if tipo_data not in TIPOS_DATA:
            raise ValueError(
                f"tipo_data inválido: {tipo_data!r}; use {sorted(TIPOS_DATA)}"
            )
        if por_pagina not in (10, 30, 50):
            raise ValueError(
                f"por_pagina inválido: {por_pagina!r}; o portal aceita 10, 30 ou 50"
            )
        avancados = {k: v.strip() for k, v in filtros_avancados.items() if v and v.strip()}
        desconhecidos = set(avancados) - set(CAMPOS_AVANCADOS)
        if desconhecidos:
            raise ValueError(
                f"filtros avançados desconhecidos: {sorted(desconhecidos)}; "
                f"use {sorted(CAMPOS_AVANCADOS)}"
            )
        if avancados and base != "trf1":
            raise ValueError(
                "filtros avançados só estão disponíveis na base 'trf1'"
            )
        for k in ("data_inicio", "data_fim"):
            if k in avancados:
                avancados[k] = _normalizar_data(avancados[k], k)

        async with self._sessao() as sessao:
            m = _RE_VIEWSTATE.search(await sessao.abrir(url))
            if not m:
                raise ConsultaFalhou("ViewState não encontrado na página inicial")
            viewstate = m.group(1)

            campos_do_portal: dict[str, str] = {}
            if avancados:
                viewstate, campos_do_portal = await self._ligar_pesquisa_avancada(
                    sessao, url, viewstate, tipos, fontes
                )

            form: dict = {
                "formulario": "formulario",
                "formulario:textoLivre": query,
                "formulario:actPesquisar": "",
                "javax.faces.ViewState": viewstate,
            }
            if base == "trf1":
                form["formulario:selectTiposDocumento"] = tipos
                form["formulario:j_idt62"] = fontes
            if avancados:
                form["formulario:ckbAvancada_input"] = "on"
                form["formulario:combo_tipo_data_input"] = TIPOS_DATA[tipo_data]
                for chave, valor in avancados.items():
                    campo = campos_do_portal.get(chave) or CAMPOS_AVANCADOS[chave]
                    form[campo] = valor

            pagina_html = await sessao.enviar(url, form)

            total = self._extrair_total(pagina_html)

            # a página 1 com 30/página já veio no POST da busca; qualquer outra
            # combinação (page > 1 ou tamanho diferente) exige o POST parcial
            # de paginação do PrimeFaces
            if (page > 1 or por_pagina != 30) and total > 0:
                m2 = _RE_VIEWSTATE.search(pagina_html)
                vs2 = m2.group(1) if m2 else viewstate
                ajax = dict(form)
                ajax.pop("formulario:actPesquisar", None)
                ajax.update(
                    {
                        "javax.faces.partial.ajax": "true",
                        "javax.faces.source": "formulario:tabelaDocumentos",
                        "javax.faces.partial.execute": "formulario:tabelaDocumentos",
                        "javax.faces.partial.render": "formulario:tabelaDocumentos",
                        "formulario:tabelaDocumentos": "formulario:tabelaDocumentos",
                        "formulario:tabelaDocumentos_pagination": "true",
                        "formulario:tabelaDocumentos_first": str(
                            (page - 1) * por_pagina
                        ),
                        "formulario:tabelaDocumentos_rows": str(por_pagina),
                        "formulario:tabelaDocumentos_rppDD": str(por_pagina),
                        "javax.faces.ViewState": vs2,
                    }
                )
                pagina_html = await sessao.ajax(url, ajax)

        return Busca(
            total_no_servidor=total,
            resultados=self._parsear_documentos(pagina_html),
        )

    async def ler_decisao(self, numero_cnj: str, base: str = "trf1") -> Decisao:
        """Localiza uma decisão pelo número CNJ (com ou sem pontuação).

        Na base trf1 usa o campo "Número" da pesquisa avançada, que indexa as
        duas formas (pontuada e só dígitos) e devolve match exato. Na base
        colegiado (sem painel avançado) cai na busca livre por dígitos — o
        motor de texto só indexa a forma SEM pontuação. Se mais de um
        documento casar (ex.: acórdão + decisão monocrática do mesmo
        processo), devolve o primeiro cujo número bate exatamente; os demais
        ficam acessíveis via buscar().
        """
        digitos = _so_digitos(numero_cnj)
        if len(digitos) < 7:
            raise ValueError(
                f"número muito curto para localizar com segurança: {numero_cnj!r}"
            )
        if base == "trf1":
            busca = await self.buscar(
                "",
                base=base,
                tipos=sorted(TIPOS_VALIDOS),
                fontes=sorted(FONTES_VALIDAS),
                numero=digitos,
            )
        else:
            busca = await self.buscar(digitos, base=base)
        if not busca.resultados:
            raise ConsultaFalhou(
                f"nenhum documento encontrado para o número {numero_cnj!r} "
                f"na base {base!r}"
            )
        for d in busca.resultados:
            if _so_digitos(d.numero) == digitos or digitos in _so_digitos(d.numero):
                return d
        return busca.resultados[0]

    async def verificar_citacao(
        self, numero_cnj: str, trecho: str, base: str = "trf1"
    ) -> dict:
        d = await self.ler_decisao(numero_cnj, base=base)
        corpo = _normalizar(f"{d.ementa}\n{d.decisao}")
        alvo = _normalizar(trecho)
        valido = bool(alvo) and alvo in corpo
        motivo = (
            "trecho encontrado literalmente na ementa/decisão publicadas no portal CJF"
            if valido
            else (
                "trecho NÃO encontrado na ementa/decisão do portal CJF. "
                "Atenção: o portal não expõe o inteiro teor em texto — se o "
                "trecho vier do voto, confira no PJe (url_inteiro_teor) antes "
                "de citar."
            )
        )
        return {
            "valido": valido,
            "motivo": motivo,
            "numero": d.numero,
            "url_inteiro_teor": d.url_inteiro_teor or None,
        }

    @staticmethod
    async def _ligar_pesquisa_avancada(
        sessao: Any,
        url: str,
        viewstate: str,
        tipos: list[str],
        fontes: list[str],
    ) -> tuple[str, dict[str, str]]:
        """Replica o clique no checkbox "Pesquisa avançada".

        O JSF só processa os campos do painel avançado se ele existir na
        árvore de componentes do servidor — o que exige este POST parcial
        (partial/ajax) antes da busca. Devolve (viewstate, campos): o
        ViewState a usar em seguida (o servidor pode renová-lo) e o
        mapeamento filtro -> name extraído do HTML do painel devolvido na
        própria resposta (vazio se o parse falhar — o chamador usa o
        fallback CAMPOS_AVANCADOS).
        """
        toggle = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": "formulario:ckbAvancada",
            "javax.faces.partial.execute": "formulario:ckbAvancada",
            "javax.faces.partial.render": "formulario:pesquisaAvancada",
            "javax.faces.behavior.event": "change",
            "javax.faces.partial.event": "change",
            "formulario": "formulario",
            "formulario:textoLivre": "",
            "formulario:ckbAvancada_input": "on",
            "formulario:selectTiposDocumento": tipos,
            "formulario:j_idt62": fontes,
            "javax.faces.ViewState": viewstate,
        }
        resposta = await sessao.ajax(url, toggle)
        m = _RE_VIEWSTATE_XML.search(resposta)
        return (m.group(1) if m else viewstate), _mapear_campos_avancados(resposta)

    # ------------------------------------------------------------------ parsing

    @staticmethod
    def _extrair_total(pagina: str) -> int:
        contadores = re.findall(r"(\d+) Documento\(s\) encontrado", pagina)
        if contadores:
            return sum(int(c) for c in contadores)
        m = re.search(r"Exibindo [\d\s\-]+ de\s+(\d+)", pagina)
        return int(m.group(1)) if m else 0

    @classmethod
    def _parsear_documentos(cls, pagina: str) -> list[Decisao]:
        partes = _RE_DOC_SPLIT.split(pagina)
        # partes = [antes, id1, bloco1, id2, bloco2, ...]
        vistos: set[str] = set()
        docs: list[Decisao] = []
        for i in range(1, len(partes) - 1, 2):
            doc_id, bloco = partes[i], partes[i + 1]
            if doc_id in vistos:
                continue
            vistos.add(doc_id)
            docs.append(cls._parsear_bloco(doc_id, bloco))
        return docs

    @classmethod
    def _parsear_bloco(cls, doc_id: str, bloco: str) -> Decisao:
        # remove os <script> PrimeFaces espalhados pelo bloco
        bloco = re.sub(r"<script.*?</script>", "", bloco, flags=re.S)
        d = Decisao(doc_id=doc_id)

        # o href do inteiro teor vem com HTML escapado dentro do atributo
        m_url = re.search(
            r'href="[^"]*?(https?://[^"&]+)', html_lib.unescape(bloco)
        )
        if m_url:
            d.url_inteiro_teor = m_url.group(1)

        texto = _RE_TAGS.sub("\n", bloco)
        texto = html_lib.unescape(texto)
        linhas = [ln.strip() for ln in texto.split("\n")]
        linhas = [ln for ln in linhas if ln]

        rotulo_para_attr = dict(_CAMPOS)
        i = 0
        while i < len(linhas):
            rotulo = linhas[i]
            if rotulo in rotulo_para_attr:
                attr = rotulo_para_attr[rotulo]
                valor_linhas: list[str] = []
                j = i + 1
                while j < len(linhas) and linhas[j] not in _FIM_DE_CAMPO:
                    if linhas[j].startswith("PrimeFaces."):
                        break
                    valor_linhas.append(linhas[j])
                    j += 1
                valor = "\n".join(valor_linhas).strip()
                if attr in ("ementa", "decisao"):
                    # reconstitui o fluxo do texto (o portal quebra linhas
                    # no meio de frases por causa das marcações de destaque)
                    valor = re.sub(r"\s*\n\s*", " ", valor)
                    valor = re.sub(r"\s{2,}", " ", valor)
                elif attr == "numero":
                    # o portal exibe o número 2x (pontuado + só dígitos);
                    # fica só a forma pontuada, canônica para citação
                    valor = valor.split("\n")[0].strip()
                else:
                    # campos "de uma linha" podem vir quebrados em várias
                    # (ex.: "DESEMBARGADOR FEDERAL" / "NEY BELLO") — a captura
                    # já parou no próximo rótulo, então é seguro juntar tudo
                    valor = re.sub(r"\s*\n\s*", " ", valor).strip()
                if not getattr(d, attr):
                    setattr(d, attr, valor)
                i = j
            else:
                i += 1
        return d
