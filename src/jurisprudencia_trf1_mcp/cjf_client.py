"""Cliente HTTP para o portal de jurisprudência do CJF (jurisprudencia.cjf.jus.br).

O portal é um app JSF/PrimeFaces 6.2 sem Cloudflare nem captcha na busca.
Fluxo por consulta (stateless — cada busca abre sessão nova):
  1. GET  {base}/index.xhtml            -> cookie JSESSIONID + javax.faces.ViewState
  2. POST {base}/index.xhtml (form)     -> página 1 de resultados (HTML completo)
  3. POST parcial PrimeFaces (opcional) -> páginas seguintes (first=N*30)

Todo o conteúdo da decisão (número, classe, relator, órgão, datas, ementa,
dispositivo) vem inline no HTML dos resultados; o "inteiro teor" do portal é
apenas um link para a consulta pública do PJe.
"""
from __future__ import annotations

import html as html_lib
import re
import unicodedata
from dataclasses import dataclass, field

import httpx

BASES = {
    "trf1": "https://jurisprudencia.cjf.jus.br/trf1/index.xhtml",
    "colegiado": "https://jurisprudencia.cjf.jus.br/colegiado/index.xhtml",
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


class CJFClient:
    def __init__(self, timeout: float = 40.0):
        self._timeout = timeout

    def _novo_http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"User-Agent": _UA},
            timeout=self._timeout,
            follow_redirects=True,
        )

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

        async with self._novo_http() as http:
            r = await http.get(url)
            r.raise_for_status()
            m = _RE_VIEWSTATE.search(r.text)
            if not m:
                raise ConsultaFalhou("ViewState não encontrado na página inicial")
            viewstate = m.group(1)

            campos_do_portal: dict[str, str] = {}
            if avancados:
                viewstate, campos_do_portal = await self._ligar_pesquisa_avancada(
                    http, url, viewstate, tipos, fontes
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

            r2 = await http.post(url, data=form)
            r2.raise_for_status()
            pagina_html = r2.text

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
                r3 = await http.post(
                    url,
                    data=ajax,
                    headers={
                        "Faces-Request": "partial/ajax",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                )
                r3.raise_for_status()
                pagina_html = r3.text

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
        http: httpx.AsyncClient,
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
        r = await http.post(
            url,
            data=toggle,
            headers={
                "Faces-Request": "partial/ajax",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        r.raise_for_status()
        m = _RE_VIEWSTATE_XML.search(r.text)
        return (m.group(1) if m else viewstate), _mapear_campos_avancados(r.text)

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
