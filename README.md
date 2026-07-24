<h1 align="center">
    <img alt="MCP Jurisprudência TRF1" src="https://raw.githubusercontent.com/fxbarros/MCP-TRF1-Jurisprudencia/main/docs/assets/banner.svg?sanitize=true">
    <br>
    <small>Precedentes da Justiça Federal da 1ª Região direto do portal oficial do CJF — busca com operadores booleanos, filtros de pesquisa avançada e citação conferida contra a fonte antes de ir para a peça</small>
</h1>

<p align="center">
    <img alt="Python" src="https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white">
    <img alt="Ferramentas" src="https://img.shields.io/badge/ferramentas-3-brightgreen">
    <img alt="Bases" src="https://img.shields.io/badge/bases-TRF1%20%2B%20JEF1%20%2B%20Colegiado%20CJF-blueviolet">
    <img alt="MCP" src="https://img.shields.io/badge/MCP-Claude%20Desktop%20%2B%20Code-d97757">
    <img alt="Acesso" src="https://img.shields.io/badge/acesso-sem%20login%2C%20sem%20captcha-black">
    <img alt="HTTP puro" src="https://img.shields.io/badge/motor-httpx%20puro%2C%20sem%20browser-8b0000">
</p>

Servidor [MCP](https://modelcontextprotocol.io) que permite ao Claude pesquisar a **jurisprudência do TRF1** (Tribunal Regional Federal da 1ª Região), das **Turmas Recursais/JEF1** e do **Colegiado do CJF** em linguagem natural, consultando o portal oficial [jurisprudencia.cjf.jus.br](https://jurisprudencia.cjf.jus.br) — sem login, sem captcha e sem navegador.

Complementa o [MCP PJe-TRF1](https://github.com/fxbarros/MCP-PJe-TRF1): este pesquisa **precedentes** (ementas e dispositivos); aquele consulta **processos** (autos, prazos e documentos). O inteiro teor do voto fica no PJe — cada resultado traz o link, e o MCP PJe-TRF1 lê o conteúdo.

## ✨ Funcionalidades

- 🔎 **Busca livre com os operadores do portal**: `e`, `ou`, `não`, `proxN`, `com`, `mesmo`, radical `$` e expressão exata
- 🎛️ **Pesquisa avançada completa**: período de julgamento ou publicação, relator, órgão julgador, classe, origem, número do processo, referência legislativa e busca restrita à ementa
- 🧾 **Citação ABNT pronta** para colar na peça
- ✅ **Verificação literal de citações**: confere se o trecho que você quer citar entre aspas existe mesmo na ementa/dispositivo publicados — antídoto contra alucinação
- 📚 **Três bases num só servidor**: acórdãos do TRF1, Turmas Recursais (JEF1) e Colegiado do CJF (TNU e afins)
- ⚡ **HTTP puro** (`httpx`): cada consulta são 2–4 requests; nada de Playwright/Chromium

## 🛠️ As 3 ferramentas

| Ferramenta | O que faz |
|---|---|
| `buscar_jurisprudencia` | pesquisa livre + filtros avançados; tipos acórdão/súmula/arguição/decisão monocrática; paginação com até 50 por página |
| `ler_decisao` | localiza uma decisão pelo nº CNJ (com ou sem pontuação) e devolve metadados, ementa, dispositivo e citação ABNT |
| `verificar_citacao` | confere se um trecho aparece literalmente na ementa/dispositivo antes de citar entre aspas |

### Operadores de busca

| Operador | Efeito |
|---|---|
| espaço | expressão exata (`desapropriação indireta`) |
| `e` | ambos os termos no documento |
| `mesmo` | ambos no mesmo campo (ex.: ambos na ementa) |
| `com` | ambos no mesmo parágrafo |
| `prox5` | até 5 palavras de distância |
| `ou` | qualquer um dos termos |
| `não` | exclui o termo |
| `termo$` | radical (`desapropria$` acha desapropriação, desapropriado…) |

Amplitude: `e` ⊇ `mesmo` ⊇ `com` ⊇ `proxN` — comece amplo, aperte se vier ruído.

### Filtros da pesquisa avançada

`data_inicio`/`data_fim` (julgamento **ou** publicação), `relator`, `orgao_julgador`, `classe`, `origem`, `numero`, `referencia_legislativa` e `ementa_decisao` — combináveis entre si e com a busca livre.

> ⚠️ `referencia_legislativa` é um índice manual do portal com cobertura parcial: zero resultados ali **não** significa que não há acórdãos sobre a norma — refaça como busca livre.

## 💬 Exemplos de uso

```
Busque acórdãos do TRF1 sobre desapropriação indireta julgados em 2024

O que a Quarta Turma decidiu sobre improbidade administrativa com relatoria do Ney Bello?

Súmulas do TRF1 sobre execução fiscal

Jurisprudência da TNU sobre benefício por incapacidade (base colegiado)

Leia a decisão 1000320-75.2023.4.01.9340 e me dê a citação ABNT

Antes de eu citar, verifique se este trecho consta da ementa: "..."
```

## 🏗️ Arquitetura

O portal é um app **JSF/PrimeFaces** sem Cloudflare nem captcha na busca. Cada consulta abre sessão nova:

1. `GET index.xhtml` → cookie + `ViewState`
2. *(se houver filtro avançado)* `POST` parcial que "liga" o painel de pesquisa avançada — o JSF só processa campos que existem na árvore de componentes do servidor; sem esse handshake os filtros são silenciosamente ignorados
3. `POST` do formulário → página de resultados (ementa e dispositivo vêm inline no HTML)
4. *(se `page > 1` ou 50/página)* `POST` parcial de paginação do PrimeFaces

Os `name` dos inputs do painel avançado (`j_idtNN`) mudam entre deploys do portal — por isso o mapeamento é extraído em tempo de execução dos atributos estáveis dos `<label>`, com fallback num dicionário fixo.

```
jurisprudencia-trf1-mcp/
├── README.md
├── pyproject.toml
├── uv.lock
├── docs/assets/banner.svg
└── src/jurisprudencia_trf1_mcp/
    ├── server.py        # servidor MCP (3 tools, FastMCP)
    └── cjf_client.py    # cliente httpx do portal (handshake JSF, parsing, paginação)
```

## 🧰 Requisitos

- Python 3.12+ e [uv](https://docs.astral.sh/uv/)
- Claude Desktop e/ou Claude Code

## 📦 Instalação

```bash
git clone https://github.com/fxbarros/MCP-TRF1-Jurisprudencia.git
cd MCP-TRF1-Jurisprudencia
uv sync
```

### Claude Desktop

No `claude_desktop_config.json`:

```json
"jurisprudencia-trf1": {
  "command": "uv",
  "args": [
    "--directory", "/caminho/para/MCP-TRF1-Jurisprudencia",
    "run", "jurisprudencia-trf1-mcp"
  ]
}
```

Depois `Cmd+Q` e abra o Claude Desktop de novo.

### Claude Code

```bash
claude mcp add --scope user jurisprudencia-trf1 -- uv --directory /caminho/para/MCP-TRF1-Jurisprudencia run jurisprudencia-trf1-mcp
```

## ⚠️ Avisos importantes

**Anti-alucinação** — a ementa e o dispositivo devolvidos vêm do portal oficial e são citáveis; ainda assim, antes de colocar qualquer trecho entre aspas numa peça, rode `verificar_citacao`. O que a verificação **não** cobre é o voto: o portal não expõe o inteiro teor em texto — o campo `url_inteiro_teor` aponta para a consulta pública do PJe.

**Fragilidade de scraping** — o projeto depende do HTML atual do portal. O mapeamento dinâmico dos campos absorve renomeações de `j_idtNN`, mas uma reforma maior do layout pode exigir ajuste no `cjf_client.py`.

**Uso responsável** — consultas pontuais de pesquisa jurídica; nada de varredura massiva do portal.

## 📝 Licença e créditos

Uso pessoal e profissional, sem garantias — use por sua conta e risco. Construído por [Fábio Ximenes Barros](https://github.com/fxbarros) com ajuda do [Claude](https://www.anthropic.com/claude), usando [httpx](https://www.python-httpx.org) e o [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).

<p align="center"><sub>Arte do banner: original — marca dos projetos MCP do autor.</sub></p>
