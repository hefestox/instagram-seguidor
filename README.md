# Instagram

Repositório local para o projeto NichoPost / Instagram com backend Flask e frontend HTML.

## Conteúdo

- `app.py` - servidor Flask
- `nichopost_usdt_platform.html` - interface do usuário
- `nichopost.db` - banco de dados SQLite local (não versionado)
- `nichopost_env/` - ambiente virtual (não versionado)
- `uploads/` - arquivos enviados pelo usuário (não versionado)

## Uso

1. Ativar ambiente virtual:

```powershell
cd "c:\Users\LENOVO\Desktop\instragram + seguidor"
.\nichopost_env\Scripts\Activate.ps1
```

2. Instalar dependências:

```powershell
pip install flask werkzeug
```

3. Executar:

```powershell
python app.py
```

4. Abrir no navegador:

- `http://127.0.0.1:5000`

## Deploy no Railway

1. Confirme que os arquivos `requirements.txt` e `Procfile` estão no repositório.
2. Faça commit e push para o GitHub.
3. No Railway, crie um novo projeto e escolha Deploy from GitHub.
4. Selecione o repositório `hefestox/instagram-seguidor`.
5. Defina `SECRET_KEY` como uma string segura (opcional, mas recomendado).
6. Railway configura `PORT` automaticamente.
7. Inicie o deploy e abra a URL gerada pelo Railway.

> Observação: SQLite funciona em Railway para testes, mas o banco pode ser refeito em cada redeploy. Para produção, prefira um banco externo.
