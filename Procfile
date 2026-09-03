# 1 worker enquanto a base de dados for SQLite: o BEGIN IMMEDIATE serializa as
# marcações DENTRO do processo. Vários workers sobre o mesmo ficheiro SQLite =
# "database is locked" e corridas fora da cobertura dos testes.
# Ao migrar para Postgres (ver MIGRATION.md), subir --workers.
# As migrações correm sozinhas à 1.ª ligação (db.garantir_migracoes); o
# render.yaml também as corre num preDeployCommand.
web: gunicorn bot:app --workers 1 --threads 8 --timeout 120
