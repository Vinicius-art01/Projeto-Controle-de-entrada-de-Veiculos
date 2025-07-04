import sqlite3
from contextlib import closing

def init_db():
    with closing(sqlite3.connect('solicitacoes.db')) as conn:
        with conn:  # Auto-commit
            conn.execute('''
                CREATE TABLE IF NOT EXISTS solicitacoes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dados TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pendente',
                    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

def add_solicitacao(dados):
    with closing(sqlite3.connect('solicitacoes.db')) as conn:
        with conn:
            conn.execute(
                "INSERT INTO solicitacoes (dados, status) VALUES (?, ?)",
                (dados, 'pendente')
            )

def get_solicitacoes_pendentes():
    with closing(sqlite3.connect('solicitacoes.db')) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, dados, criado_em FROM solicitacoes "
            "WHERE status = 'pendente' ORDER BY criado_em"
        )
        return cursor.fetchall()

def atualizar_status(id, novo_status):
    with closing(sqlite3.connect('solicitacoes.db')) as conn:
        with conn:
            conn.execute(
                "UPDATE solicitacoes SET status = ? WHERE id = ?",
                (novo_status, id)
            )