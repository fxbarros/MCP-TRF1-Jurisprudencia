"""Servidor MCP para o portal de jurisprudência do CJF (TRF1/JEF1 e Colegiado).

Expõe três tools ao Claude Desktop:
- buscar_jurisprudencia: pesquisa livre com operadores do portal
- ler_decisao: localiza uma decisão pelo número CNJ
- verificar_citacao: confere se um trecho aparece na ementa/decisão
"""
from __future__ import annotations

import asyncio
from typing import Optional

from mcp.server.fastmcp import FastMCP

from jurisprudencia_trf1_mcp.cjf_client import CJFClient, ConsultaFalhou


def _erro(msg: str) -> dict:
    return {
        "erro": (
            f"Falha ao consultar o portal de jurisprudência do CJF: {msg}. "
            "O site pode estar lento ou fora do ar. NÃO invente resultados — "
            "avise o usuário e sugira tentar novamente em instantes."
        ),
    }


mcp = FastMCP("jurisprudencia-trf1")

_client: Optional[CJFClient] = None
_client_lock = asyncio.Lock()


async def _get_client() -> CJFClient:
    global _client
    async with _client_lock:
        if _client is None:
            _client = CJFClient()
        return _client


@mcp.tool()
async def buscar_jurisprudencia(
    query: str = "",
    limite: int = 10,
    page: int = 1,
    tipos: Optional[list[str]] = None,
    fontes: Optional[list[str]] = None,
    base: str = "trf1",
    data_inicio: str = "",
    data_fim: str = "",
    tipo_data: str = "julgamento",
    relator: str = "",
    orgao_julgador: str = "",
    classe: str = "",
    origem: str = "",
    numero: str = "",
    referencia_legislativa: str = "",
    ementa_decisao: str = "",
) -> dict:
    """Pesquisa jurisprudência do TRF1/JEF1 (ou Colegiado CJF) por palavra-chave.

    Consulta o portal oficial jurisprudencia.cjf.jus.br (sem captcha). O
    conteúdo devolvido — ementa completa, dispositivo, relator, datas — vem
    diretamente do portal oficial.

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    SINTAXE DA QUERY — operadores do portal CJF (escreva-os NA query)
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
      termo1 e termo2      → ambos no documento (AND). Espaço simples entre
                             palavras JÁ equivale a "adj" (expressão exata).
      termo1 ou termo2     → qualquer um (OR)
      não termo            → exclui
      termo1 prox5 termo2  → até N palavras de distância
      termo1 com termo2    → ambos no MESMO parágrafo
      termo1 mesmo termo2  → ambos no MESMO campo (ex.: ambos na ementa)
      termo$               → radical (desapropria$ acha desapropriação etc.)
    Amplitude: e ⊇ mesmo ⊇ com ⊇ proxN (do mais amplo ao mais restrito).
    Ex.: "desapropriação indireta" busca a expressão exata;
         "desapropriação e indígena" exige as duas palavras.

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ANTI-ALUCINAÇÃO
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    A ementa vem do próprio portal oficial e é citável. Antes de citar trecho
    entre aspas em peça, rode verificar_citacao. O inteiro teor (voto) NÃO é
    exposto em texto pelo portal — o campo url_inteiro_teor aponta para a
    consulta pública do PJe (para o conteúdo do voto, use o MCP pje-trf1).

    Args:
        query: termos de busca com os operadores acima. Pode ficar vazia se
               algum filtro avançado for usado.
        limite: máximo de resultados devolvidos (1-50). Default 10.
        page: página de resultados. Default 1. O tamanho da página acompanha
              `limite`: 50 quando limite > 30, senão 30 — mantenha o MESMO
              limite ao paginar para não pular/repetir resultados.
        tipos: tipos de documento — lista com "ACORDAO" (default), "SUMULA",
               "ARGUICAO" e/ou "DECISAOMONO". Ignorado quando base="colegiado".
        fontes: "TRF1" e/ou "JEF1" (Turmas Recursais/JEFs). Default: ambas,
                como no portal — cada resultado traz `orgao_julgador`/`origem`
                para pesar a autoridade do precedente. Ignorado quando
                base="colegiado".
        base: "trf1" (default) ou "colegiado" (jurisprudência do Colegiado do
              CJF — TNU e afins). Filtros avançados só na base "trf1".
        data_inicio: início do período (DD/MM/AAAA ou AAAA-MM-DD).
        data_fim: fim do período (mesmos formatos).
        tipo_data: a que data o período se refere — "julgamento" (default) ou
                   "publicacao".
        relator: nome (ou parte) do relator. Filtro do portal, não busca livre.
        orgao_julgador: ex. "QUARTA TURMA", "CORTE ESPECIAL".
        classe: sigla/nome da classe processual (ex. "AC", "ApCiv", "AG").
        origem: seção/subseção de origem (ex. "PI", "SEÇÃO JUDICIÁRIA DO PIAUÍ").
        numero: número do processo (restringe a busca ao campo Número).
        referencia_legislativa: acórdãos indexados a uma norma específica
               (ex. "lei 8429"). Índice manual do portal — pouca cobertura;
               zero aqui NÃO significa que não há acórdãos sobre a norma.
        ementa_decisao: termos buscados APENAS na ementa/dispositivo (a query
               livre varre o documento inteiro).

    Returns:
        Dict com resultados (doc_id, tipo, numero [CNJ], classe, relator,
        orgao_julgador, data_julgamento, data_publicacao, ementa),
        total_retornado, total_no_servidor [total REAL da query — use este
        para afirmar quantidades], query_executada, page, limite.
        Campo `erro` em caso de falha.
    """
    limite = max(1, min(limite, 50))
    # o portal pagina em 10/30/50; usar 50 só quando pedido evita um POST
    # extra de repaginação nas buscas comuns
    por_pagina = 50 if limite > 30 else 30
    filtros = {
        "data_inicio": data_inicio,
        "data_fim": data_fim,
        "relator": relator,
        "orgao_julgador": orgao_julgador,
        "classe": classe,
        "origem": origem,
        "numero": numero,
        "referencia_legislativa": referencia_legislativa,
        "ementa_decisao": ementa_decisao,
    }
    filtros = {k: v for k, v in filtros.items() if v and v.strip()}
    if not query.strip() and not filtros:
        return _erro("informe uma query ou ao menos um filtro avançado")
    client = await _get_client()
    try:
        busca = await client.buscar(
            query,
            base=base,
            tipos=tipos,
            fontes=fontes,
            page=page,
            por_pagina=por_pagina,
            tipo_data=tipo_data,
            **filtros,
        )
    except (ConsultaFalhou, ValueError) as e:
        return _erro(str(e))
    except Exception as e:
        return _erro(f"{type(e).__name__}: {e}")
    itens = [d.to_dict(resumido=True) for d in busca.resultados[:limite]]
    resposta: dict = {
        "resultados": itens,
        "total_retornado": len(itens),
        "total_no_servidor": busca.total_no_servidor,
        "query_executada": query,
        "page": page,
        "limite": limite,
        "base": base,
    }
    if filtros:
        resposta["filtros_aplicados"] = dict(filtros, tipo_data=tipo_data)
    if len(itens) == 0:
        resposta["_aviso"] = (
            "ZERO RESULTADOS. Reformule a query (radical com $, sinônimos, "
            "operador 'ou') antes de relatar 'nada encontrado'."
        )
    return resposta


@mcp.tool()
async def ler_decisao(numero_cnj: str, base: str = "trf1") -> dict:
    """Lê uma decisão do TRF1/JEF1 pelo número CNJ e extrai os metadados formais.

    Use SEMPRE antes de citar uma decisão em peça processual. Devolve ementa
    completa, dispositivo (campo `decisao`), relator, órgão julgador, datas e
    a citação ABNT pronta.

    Args:
        numero_cnj: número do processo, com ou sem pontuação
                    (ex.: 1037242-05.2023.4.01.0000).
        base: "trf1" (default) ou "colegiado".

    Returns:
        Dict com metadados + ementa + decisao + url_inteiro_teor (link da
        consulta pública do PJe; o texto do voto não é exposto pelo portal —
        para lê-lo use o MCP pje-trf1) + citacao_abnt.
    """
    client = await _get_client()
    try:
        d = await client.ler_decisao(numero_cnj, base=base)
    except (ConsultaFalhou, ValueError) as e:
        return _erro(str(e))
    except Exception as e:
        return _erro(f"{type(e).__name__}: {e}")
    out = d.to_dict()
    out["citacao_abnt"] = d.citacao_abnt()
    return out


@mcp.tool()
async def verificar_citacao(numero_cnj: str, trecho: str, base: str = "trf1") -> dict:
    """Confere se um trecho aparece literalmente na ementa/decisão publicadas.

    ⚠️ USE SEMPRE antes de incluir citação direta entre aspas. Se `valido:
    False`, NÃO cite — reescreva como paráfrase ou confira o voto no PJe.
    Comparação tolerante a acentos, caixa e espaços; intolerante a
    substituição/omissão de palavras. A verificação cobre APENAS ementa e
    dispositivo (o portal não expõe o texto do voto).

    Args:
        numero_cnj: número do processo (mesmo formato de ler_decisao).
        trecho: texto que se pretende citar entre aspas.
        base: "trf1" (default) ou "colegiado".

    Returns:
        Dict com valido (bool), motivo (str), numero, url_inteiro_teor.
    """
    client = await _get_client()
    try:
        return await client.verificar_citacao(numero_cnj, trecho, base=base)
    except (ConsultaFalhou, ValueError) as e:
        return _erro(str(e))
    except Exception as e:
        return _erro(f"{type(e).__name__}: {e}")


def main() -> None:
    """Entry point pro `uv run jurisprudencia-trf1-mcp`."""
    mcp.run()


if __name__ == "__main__":
    main()
