# LSEE · Alocação de Recursos (Web / Vercel + MySQL)

Sistema web para consulta pública e gestão administrativa de computadores, mapa físico do laboratório, pessoas, fila de prioridade, simulador de upgrade, monitores, etiquetas de patrimônio e rede do LSEE.

## Requisitos

- Python 3.10+
- Banco de dados MySQL 8+ (configurado via `.env` localmente ou Environment Variables na Vercel)

Instalação das dependências:

```powershell
pip install -r requirements.txt
```

## Variáveis de Ambiente

Copie `.env.example` para `.env` e preencha as credenciais do banco MySQL:

```env
DB_HOST=
DB_PORT=3306
DB_USER=
DB_PASSWORD=
DB_NAME=
LSEE_SESSION_SECRET=
```

> **Segurança:** Nenhuma senha administrativa estática é armazenada em `.env` ou no repositório. A senha do administrador é salva exclusivamente como hash pós-quântico (`scrypt` 512 bits) no MySQL, e sessões usam `HMAC-SHA512`.

## Como executar localmente

```powershell
python server.py --host 127.0.0.1 --port 8080
```

Acesse no navegador: `http://127.0.0.1:8080`

## Senha do Administrador

Para definir ou alterar a senha do administrador no MySQL a qualquer momento:

```powershell
python server.py --set-password
```

## Deploy na Vercel

1. Importe o repositório na Vercel (entrypoint em `api/index.py`, configurado via `vercel.json`).
2. Configure as Environment Variables no painel da Vercel: `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` (e opcionalmente `LSEE_SESSION_SECRET`).
3. Faça o deploy.

## Principais Módulos

- **Visão Pública (`/api/public/*`):** **Dashboard**, **Computadores**, **Mapa do LSEE** (somente leitura e anonimizado), **Simulador** de upgrade e modal **Regras de Uso**.
- **Modo Administrador (`/api/admin/*`):** **Instruções**, edição de **Regras de Uso**, **Critérios** (edição manual de pesos das notas, regras gerais, faixas de RAM, prioridades por categoria e benchmarks de CPU com protocolo CPU-Z), **Pessoas**, **Alocações** (com checklist **Tutorial: Liberar PC**), **Monitores**, **Patrimônios** (inventário patrimonial do laboratório com filtros, soma de valores, controle de garantia, importação de relatórios CSV do Mercúrio/USP e exportação CSV), **Etiquetas** (PCs, Monitores, Placas e Livre em PDF 1:1 e `.docx`), **Rede** (IP/MAC com exportação e importação `.xlsx`), **Solicitações** (material de consumo EESC/USP em PDF e `.docx` com histórico), edição do **Mapa do LSEE** e **Parâmetros & Log**.
