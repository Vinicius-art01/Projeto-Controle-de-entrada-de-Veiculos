import sys
print("Python path:", sys.executable)
print("Reportlab path:", [p for p in sys.path if 'reportlab' in p])

import os
import json
import re
import sqlite3
from datetime import datetime, timedelta
from collections import Counter
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response
from flask_login import (
    LoginManager, UserMixin, login_user,
    login_required, logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from io import BytesIO
from contextlib import closing

# Tenta importar o reportlab com fallback
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    PDF_SUPPORTED = True
except ImportError:
    PDF_SUPPORTED = False
    print("Aviso: Módulo reportlab não instalado. Exportação PDF desabilitada.")

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret')

# --- Arquivos de dados locais ---
USERS_FILE = os.path.join(os.path.dirname(__file__), "users.json")
HIST_FILE = os.path.join(os.path.dirname(__file__), "history.json")
DB_FILE = os.path.join(os.path.dirname(__file__), "solicitacoes.db")  # Arquivo do banco para pendentes

# Inicialização do banco de dados para solicitações pendentes
def init_db():
    with closing(sqlite3.connect(DB_FILE)) as conn:
        with conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS solicitacoes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filial TEXT NOT NULL,
                    ordem INTEGER,
                    tipo TEXT NOT NULL,
                    placa TEXT NOT NULL,
                    oleo INTEGER DEFAULT 0,
                    filtro_oleo INTEGER DEFAULT 0,
                    filtro_combustivel INTEGER DEFAULT 0,
                    filtro_ar_motor INTEGER DEFAULT 0,
                    filtro_ar_condicionado INTEGER DEFAULT 0,
                    diferencial INTEGER DEFAULT 0,
                    caixa INTEGER DEFAULT 0,
                    vendedor TEXT NOT NULL,
                    cliente TEXT,
                    trocador TEXT,
                    hora_entrada TEXT
                )
            ''')
            # Cria índice para melhor performance
            conn.execute('CREATE INDEX IF NOT EXISTS idx_filial ON solicitacoes (filial)')

init_db()  # Garante que a tabela existe

def carregar_usuarios():
    with open(USERS_FILE, encoding='utf-8') as f:
        dados = json.load(f)
    usuarios = {}
    for u in dados:
        usuarios[u['matricula']] = {
            'senha_hash': generate_password_hash(u['senha']),
            'nome': u['nome'],
            'role': u.get('role', 'user'),
            'filial': u.get('filial')
        }
    return usuarios

def carregar_historico_local():
    if not os.path.exists(HIST_FILE):
        return []
    try:
        with open(HIST_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        # Se o arquivo estiver corrompido ou vazio, recria com uma lista vazia
        with open(HIST_FILE, 'w', encoding='utf-8') as f:
            json.dump([], f, ensure_ascii=False, indent=2)
        return []

def salvar_historico_local(registros):
    with open(HIST_FILE, 'w', encoding='utf-8') as f:
        json.dump(registros, f, ensure_ascii=False, indent=2)

USERS = carregar_usuarios()

@app.context_processor
def inject_year():
    return {'current_year': datetime.now().year}

login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

class User(UserMixin):
    def __init__(self, matricula, nome, role, filial=None):
        self.id = matricula
        self.nome = nome
        self.role = role
        self.filial = filial

@login_manager.user_loader
def load_user(user_id):
    u = USERS.get(user_id)
    if not u:
        return None
    return User(user_id, u['nome'], u['role'], u.get('filial'))

# --- Validação de placa ---
def validar_placa(placa: str) -> bool:
    placa = re.sub(r'\s+', '', placa.upper())
    return bool(re.match(r'^[A-Z]{3}[0-9][A-Z0-9][0-9]{2}$', placa))

# --- Filiais ---
FILIAIS = ['RIO','SALVADOR','AVENIDA','DUTRA','SEDE','ITABUNA']

# --- Funções de banco de dados para solicitações pendentes ---
def adicionar_solicitacao(filial, dados):
    with closing(sqlite3.connect(DB_FILE)) as conn:
        with conn:
            cursor = conn.cursor()
            # Obter a próxima ordem
            cursor.execute(
                "SELECT MAX(ordem) FROM solicitacoes WHERE filial = ?",
                (filial,)
            )
            max_ordem = cursor.fetchone()[0]
            nova_ordem = 1 if max_ordem is None else max_ordem + 1

            cursor.execute(
                '''
                INSERT INTO solicitacoes (
                    filial, ordem, tipo, placa, oleo, filtro_oleo, filtro_combustivel,
                    filtro_ar_motor, filtro_ar_condicionado, diferencial, caixa,
                    vendedor, cliente, trocador, hora_entrada
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    filial,
                    nova_ordem,
                    dados['tipo'],
                    dados['placa'],
                    1 if dados.get('oleo', False) else 0,
                    1 if dados.get('filtro_oleo', False) else 0,
                    1 if dados.get('filtro_combustivel', False) else 0,
                    1 if dados.get('filtro_ar_motor', False) else 0,
                    1 if dados.get('filtro_ar_condicionado', False) else 0,
                    1 if dados.get('diferencial', False) else 0,
                    1 if dados.get('caixa', False) else 0,
                    dados['vendedor'],
                    dados.get('cliente', ''),
                    dados.get('trocador', ''),
                    dados['hora_entrada']
                )
            )

def obter_fila(filial):
    with closing(sqlite3.connect(DB_FILE)) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, ordem, tipo, placa, oleo, filtro_oleo, filtro_combustivel, "
            "filtro_ar_motor, filtro_ar_condicionado, diferencial, caixa, vendedor, cliente, trocador, hora_entrada "
            "FROM solicitacoes "
            "WHERE filial = ? "
            "ORDER BY ordem",
            (filial,)
        )
        colunas = [col[0] for col in cursor.description]
        fila = []
        for row in cursor.fetchall():
            veic = dict(zip(colunas, row))
            for servico in ['oleo','filtro_oleo','filtro_combustivel','filtro_ar_motor','filtro_ar_condicionado','diferencial','caixa']:
                veic[servico] = bool(veic[servico])
            fila.append(veic)
        return fila

def remover_solicitacao(filial, ordem):
    with closing(sqlite3.connect(DB_FILE)) as conn:
        with conn:
            cursor = conn.cursor()
            # Remover o registro
            cursor.execute(
                "DELETE FROM solicitacoes WHERE filial = ? AND ordem = ?",
                (filial, ordem)
            )
            
            # Reordenar os veículos restantes
            cursor.execute(
                "SELECT id FROM solicitacoes WHERE filial = ? ORDER BY ordem",
                (filial,)
            )
            rows = cursor.fetchall()
            for index, (row_id,) in enumerate(rows, start=1):
                cursor.execute(
                    "UPDATE solicitacoes SET ordem = ? WHERE id = ?",
                    (index, row_id)
                )
            return True

def obter_registro_por_ordem(filial, ordem):
    with closing(sqlite3.connect(DB_FILE)) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, ordem, tipo, placa, oleo, filtro_oleo, filtro_combustivel, "
            "filtro_ar_motor, filtro_ar_condicionado, diferencial, caixa, vendedor, cliente, trocador, hora_entrada "
            "FROM solicitacoes "
            "WHERE filial = ? AND ordem = ?",
            (filial, ordem)
        )
        colunas = [col[0] for col in cursor.description]
        row = cursor.fetchone()
        if row:
            reg = dict(zip(colunas, row))
            for servico in ['oleo','filtro_oleo','filtro_combustivel','filtro_ar_motor','filtro_ar_condicionado','diferencial','caixa']:
                reg[servico] = bool(reg[servico])
            return reg
        return None

def atualizar_registro(filial, ordem, novos_dados):
    with closing(sqlite3.connect(DB_FILE)) as conn:
        with conn:
            cursor = conn.cursor()
            # Verificar se a placa nova já existe (outro veículo pendente)
            cursor.execute(
                "SELECT id FROM solicitacoes WHERE filial = ? AND placa = ? AND ordem != ?",
                (filial, novos_dados['placa'], ordem)
            )
            if cursor.fetchone():
                return False, "Esta placa já está na fila para outro veículo."

            # Atualizar
            cursor.execute(
                '''
                UPDATE solicitacoes SET
                    tipo = ?,
                    placa = ?,
                    oleo = ?,
                    filtro_oleo = ?,
                    filtro_combustivel = ?,
                    filtro_ar_motor = ?,
                    filtro_ar_condicionado = ?,
                    diferencial = ?,
                    caixa = ?,
                    cliente = ?,
                    trocador = ?
                WHERE filial = ? AND ordem = ?
                ''',
                (
                    novos_dados['tipo'],
                    novos_dados['placa'],
                    1 if novos_dados.get('oleo', False) else 0,
                    1 if novos_dados.get('filtro_oleo', False) else 0,
                    1 if novos_dados.get('filtro_combustivel', False) else 0,
                    1 if novos_dados.get('filtro_ar_motor', False) else 0,
                    1 if novos_dados.get('filtro_ar_condicionado', False) else 0,
                    1 if novos_dados.get('diferencial', False) else 0,
                    1 if novos_dados.get('caixa', False) else 0,
                    novos_dados.get('cliente', ''),
                    novos_dados.get('trocador', ''),
                    filial,
                    ordem
                )
            )
            return True, ""

# --- Rotas de autenticação ---
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        mat = request.form['matricula'].strip()
        pwd = request.form['senha'].strip()
        u = USERS.get(mat)
        if u and check_password_hash(u['senha_hash'], pwd):
            login_user(User(mat, u['nome'], u['role'], u.get('filial')))
            return redirect(url_for('home'))
        flash('Matrícula ou senha incorretos.', 'danger')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def home():
    if current_user.role == 'admin':
        filiais_disp = FILIAIS
    else:
        filiais_disp = [current_user.filial] if current_user.filial else []
    return render_template('home.html', filiais=filiais_disp)

@app.route('/filial/<filial>', methods=['GET','POST'])
@login_required
def filial_view(filial):
    if current_user.role != 'admin' and current_user.filial != filial:
        flash('Acesso não autorizado à filial.', 'danger')
        return redirect(url_for('home'))
    if filial not in FILIAIS:
        flash('Filial inválida.', 'warning')
        return redirect(url_for('home'))

    if request.method == 'POST':
        placa   = request.form['placa'].strip().upper()
        tipo    = request.form['tipo_veiculo'].strip()
        cliente = request.form['cliente'].strip()
        troc    = request.form['trocador'].strip()
        servicos_keys = [
            'oleo','filtro_oleo','filtro_combustivel',
            'filtro_ar_motor','filtro_ar_condicionado',
            'diferencial','caixa'
        ]
        servicos = {s: (s in request.form) for s in servicos_keys}

        if not validar_placa(placa):
            flash('Placa inválida! Use o formato ABC1234 ou ABC1D34.', 'danger')
        elif any(v['placa'] == placa for v in obter_fila(filial)):
            flash('Esta placa já está na fila.', 'warning')
        elif not tipo:
            flash('Tipo do veículo é obrigatório.', 'danger')
        else:
            partes = current_user.nome.split()
            if len(partes) > 1:
                vendedor = f"{partes[0]} {partes[1]}"
            else:
                vendedor = partes[0] if partes else "N/A"

            registro = {
                'tipo': tipo,
                'placa': placa,
                **servicos,
                'vendedor': vendedor,
                'cliente': cliente,
                'trocador': troc,
                'hora_entrada': datetime.now().isoformat()
            }
            adicionar_solicitacao(filial, registro)
            flash('Veículo adicionado à fila.', 'success')
            return redirect(url_for('filial_view', filial=filial))

    fila_filial = obter_fila(filial)
    return render_template('filial.html', filial=filial, fila=fila_filial)

@app.route('/filial/<filial>/liberar/<int:ordem>', methods=['POST'])
@login_required
def liberar(filial, ordem):
    if current_user.role != 'admin' and current_user.filial != filial:
        flash('Acesso não autorizado à liberação desta filial.', 'danger')
        return redirect(url_for('home'))

    reg = obter_registro_por_ordem(filial, ordem)
    if reg:
        # Garantir que hora_entrada está presente
        if 'hora_entrada' not in reg:
            reg['hora_entrada'] = datetime.now().isoformat()
            
        # Adicionar ao histórico
        reg_hist = {
            **reg,
            'filial': filial,
            'liberado_em': datetime.now().isoformat()
        }
        historico_atual = carregar_historico_local()
        historico_atual.append(reg_hist)
        salvar_historico_local(historico_atual)
        
        # Remover da fila
        remover_solicitacao(filial, ordem)
        
        flash(f"Veículo placa {reg.get('placa','N/A')} liberado.", "success")
    else:
        flash("Registro não encontrado.", "warning")

    return redirect(url_for('filial_view', filial=filial))

@app.route('/filial/<filial>/editar/<int:ordem>', methods=['GET','POST'])
@login_required
def editar_registro(filial, ordem):
    if current_user.role != 'admin' and current_user.filial != filial:
        flash('Acesso não autorizado à edição desta filial.', 'danger')
        return redirect(url_for('home'))
    if filial not in FILIAIS:
        flash('Filial inválida.', 'warning')
        return redirect(url_for('home'))

    reg = obter_registro_por_ordem(filial, ordem)
    if not reg:
        flash('Registro não encontrado.', 'warning')
        return redirect(url_for('filial_view', filial=filial))

    if request.method == 'POST':
        novos_dados = {
            'tipo': request.form['tipo_veiculo'].strip(),
            'placa': request.form['placa'].strip().upper(),
            'cliente': request.form['cliente'].strip(),
            'trocador': request.form['trocador'].strip(),
            'oleo': 'oleo' in request.form,
            'filtro_oleo': 'filtro_oleo' in request.form,
            'filtro_combustivel': 'filtro_combustivel' in request.form,
            'filtro_ar_motor': 'filtro_ar_motor' in request.form,
            'filtro_ar_condicionado': 'filtro_ar_condicionado' in request.form,
            'diferencial': 'diferencial' in request.form,
            'caixa': 'caixa' in request.form
        }

        sucesso, mensagem = atualizar_registro(filial, ordem, novos_dados)
        if sucesso:
            flash(f'Registro #{ordem} (Placa: {novos_dados["placa"]}) atualizado.', 'success')
            return redirect(url_for('filial_view', filial=filial))
        else:
            flash(mensagem, 'danger')

    return render_template('editar_filial.html', filial=filial, registro=reg)


@app.route('/historico')
@login_required
def historico():
    if current_user.role == 'user': # Usuários comuns não acessam histórico geral
        flash('Você não tem permissão para acessar o histórico.', 'danger')
        return redirect(url_for('home'))
        
    registros = carregar_historico_local()
    
    # Filtro por filial para 'rep' (representante/responsável de filial)
    if current_user.role == 'rep':
        registros = [r for r in registros if r.get('filial') == current_user.filial]

    placa_filtro = request.args.get('placa','').strip().upper()
    vendedor_filtro = request.args.get('vendedor','').strip().lower()

    if placa_filtro:
        registros = [r for r in registros if placa_filtro in r.get('placa','')]
    if vendedor_filtro:
        registros = [r for r in registros if vendedor_filtro in r.get('vendedor','').lower()]
        
    return render_template('historico.html',
                           registros=sorted(registros, key=lambda x: x.get('liberado_em', ''), reverse=True),
                           placa_filtro=placa_filtro,
                           vendedor_filtro=vendedor_filtro,
                           user_role=current_user.role)


@app.route('/registrar_reincidencia', methods=['POST'])
@login_required
def registrar_reincidencia():
    if current_user.role not in ['admin','rep']:
        return jsonify({'success':False,'error':'Sem permissão.'}), 403
    
    data = request.get_json()
    placa_reincidencia = data.get('placa')
    liberado_em_reincidencia = data.get('liberado_em')
    justificativa = data.get('justificativa')

    if not placa_reincidencia or not liberado_em_reincidencia or not justificativa:
        return jsonify({'success':False,'error':'Dados incompletos.'}), 400

    registros_hist = carregar_historico_local()
    registro_encontrado = False
    for r_item in registros_hist:
        if r_item.get('placa') == placa_reincidencia and r_item.get('liberado_em') == liberado_em_reincidencia:
            r_item['reincidencia'] = {
                'autor': current_user.nome,
                'justificativa': justificativa,
                'registrado_em': datetime.now().isoformat()
            }
            registro_encontrado = True
            break
    
    if not registro_encontrado:
        return jsonify({'success':False,'error':'Registro não encontrado.'}), 404

    salvar_historico_local(registros_hist)
    return jsonify({'success':True})


@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.role not in ['admin','rep']:
        flash('Sem permissão para acessar o dashboard.','danger')
        return redirect(url_for('home'))
    
    # Parâmetros de filtro temporal
    periodo = request.args.get('periodo', '7')
    try:
        dias_filtro = int(periodo)
    except ValueError:
        dias_filtro = 7
    
    # Filial selecionada
    filial_selecionada = None
    if current_user.role == 'admin':
        filial_selecionada = request.args.get('filial')
    elif current_user.role == 'rep':
        filial_selecionada = current_user.filial
    
    # Data limite para o filtro
    data_limite = datetime.now() - timedelta(days=dias_filtro)
    
    # --- KPIs PRINCIPAIS ---
    total_fila = 0
    total_servicos = 0
    servicos_keys = ['oleo','filtro_oleo','filtro_combustivel','filtro_ar_motor','filtro_ar_condicionado','diferencial','caixa']
    
    # Filiais a considerar
    filiais_ativas = [filial_selecionada] if filial_selecionada else FILIAIS

    for f in filiais_ativas:
        fila_filial = obter_fila(f)
        total_fila += len(fila_filial)
        for veiculo in fila_filial:
            for servico in servicos_keys:
                if veiculo.get(servico):
                    total_servicos += 1

    # --- HISTÓRICO PARA MÉTRICAS ---
    historial_completo = carregar_historico_local()
    
    # Filtra por filial e período
    historial_filtrado = []
    tempo_total_atendimento = 0
    contador_atendimentos = 0
    
    for r in historial_completo:
        # Filtro por filial
        if filial_selecionada and r.get('filial') != filial_selecionada:
            continue
        
        # Filtro por período
        try:
            data_liberacao = datetime.fromisoformat(r['liberado_em'])
            if data_liberacao < data_limite:
                continue
        except:
            continue
        
        historial_filtrado.append(r)
        
        # Cálculo do tempo de atendimento (se disponível)
        if 'hora_entrada' in r and 'liberado_em' in r:
            try:
                entrada = datetime.fromisoformat(r['hora_entrada'])
                saida = datetime.fromisoformat(r['liberado_em'])
                tempo_atendimento = (saida - entrada).total_seconds()
                
                # Adicionar verificação para evitar divisão por zero
                if tempo_atendimento > 0:
                    tempo_total_atendimento += tempo_atendimento
                    contador_atendimentos += 1
            except Exception as e:
                print(f"Erro ao calcular tempo de atendimento: {str(e)}")
                print(f"Registro problemático: {r}")

    # Cálculo do tempo médio (com proteção contra divisão por zero)
    tempo_medio_minutos = 0
    if contador_atendimentos > 0:
        tempo_medio_minutos = tempo_total_atendimento / contador_atendimentos / 60
    
    total_reinc = sum(1 for r in historial_filtrado if 'reincidencia' in r)

    # --- GRÁFICO SEMANAL (DIAS EM PORTUGUÊS) ---
    traducao_dias = {
        'Mon': 'Seg', 'Tue': 'Ter', 'Wed': 'Qua',
        'Thu': 'Qui', 'Fri': 'Sex', 'Sat': 'Sab', 'Sun': 'Dom'
    }
    
    hoje = datetime.now().date()
    contador_atendimentos_por_dia = Counter()
    
    for r in historial_filtrado:
        if 'liberado_em' in r:
            try:
                data = datetime.fromisoformat(r['liberado_em']).date()
                # Se o período for 7 dias, mostra por dia da semana. Caso contrário, agrupa por dia/mês
                if dias_filtro <= 7:
                    if (hoje - data).days < 7:
                        dia_abrev = data.strftime('%a')
                        contador_atendimentos_por_dia[traducao_dias.get(dia_abrev, dia_abrev)] += 1
                else:
                    # Para períodos maiores, mostra a data no formato DD/MM
                    contador_atendimentos_por_dia[data.strftime('%d/%m')] += 1
            except:
                continue

    # Para período de 7 dias, garantimos todos os dias da semana
    if dias_filtro <= 7:
        labels_grafico = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sab', 'Dom']
        valores_grafico = [contador_atendimentos_por_dia.get(dia, 0) for dia in labels_grafico]
    else:
        # Ordenar as datas
        datas_ordenadas = sorted(contador_atendimentos_por_dia.keys(), key=lambda x: datetime.strptime(x, '%d/%m'))
        labels_grafico = datas_ordenadas
        valores_grafico = [contador_atendimentos_por_dia[d] for d in datas_ordenadas]

    # --- NOVAS MÉTRICAS ---
    # 1. Trocador com mais reincidências
    contador_trocadores = Counter()
    for r in historial_filtrado:
        if 'reincidencia' in r:
            trocador = r.get('trocador', 'Não informado')
            contador_trocadores[trocador] += 1
    
    trocador_mais_reincidente = contador_trocadores.most_common(1)[0] if contador_trocadores else ("Nenhum", 0)

    # 2. Top vendedores
    contador_vendedores = Counter(r['vendedor'] for r in historial_filtrado if 'vendedor' in r)
    top_vendedores = contador_vendedores.most_common(5)

    # 3. Solicitações por filial (apenas para admin sem filtro)
    contador_filiais = None
    if current_user.role == 'admin' and not filial_selecionada:
        contador_filiais = Counter(r['filial'] for r in historial_filtrado if 'filial' in r)

    # 4. Serviços mais requisitados (para gráfico de pizza)
    contador_servicos = Counter()
    for r in historial_filtrado:
        for servico in servicos_keys:
            if r.get(servico):
                nome_servico = servico.replace('_', ' ').title()
                contador_servicos[nome_servico] += 1
    servicos_mais_requisitados = contador_servicos.most_common()

    return render_template('dashboard.html',
        total_fila=total_fila,
        total_servicos=total_servicos,
        total_reincidencias=total_reinc,
        tempo_medio_minutos=round(tempo_medio_minutos, 1),
        ultima_semana_labels=labels_grafico,
        ultima_semana_values=valores_grafico,
        # Novas métricas
        trocador_mais_reincidente=trocador_mais_reincidente,
        top_vendedores=top_vendedores,
        contador_filiais=contador_filiais,
        servicos_mais_requisitados=servicos_mais_requisitados,
        # Controle de filtro
        filial_selecionada=filial_selecionada,
        periodo_selecionado=dias_filtro,
        todas_filiais=FILIAIS,
        user_role=current_user.role
    )

# Rota para exportar PDF - CORRIGIDA
@app.route('/exportar_pdf')
@login_required
def exportar_pdf():
    if current_user.role not in ['admin','rep']:
        flash('Sem permissão para exportar relatórios.','danger')
        return redirect(url_for('dashboard'))
    
    if not PDF_SUPPORTED:
        flash('Funcionalidade de PDF não disponível. Instale o reportlab.', 'danger')
        return redirect(url_for('dashboard'))
    
    try:
        # Crie um buffer para o PDF
        buffer = BytesIO()
        
        # Crie o objeto Canvas
        p = canvas.Canvas(buffer, pagesize=letter)
        width, height = letter
        
        # Configurações iniciais
        p.setTitle("Relatório de Atendimentos")
        p.setFont("Helvetica-Bold", 16)
        
        # Cabeçalho
        p.drawString(100, height - 50, "Relatório de Atendimento - Controle de Veículos")
        p.setFont("Helvetica", 12)
        p.drawString(100, height - 80, f"Data: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        p.drawString(100, height - 100, f"Usuário: {current_user.nome}")
        p.drawString(100, height - 120, f"Período: Últimos {request.args.get('periodo', '7')} dias")
        
        if request.args.get('filial'):
            p.drawString(100, height - 140, f"Filial: {request.args.get('filial')}")
        
        # Linha divisória
        p.line(100, height - 160, width - 100, height - 160)
        
        # Adicione aqui o conteúdo do relatório
        y_position = height - 190
        p.setFont("Helvetica-Bold", 14)
        p.drawString(100, y_position, "Resumo Estatístico")
        p.setFont("Helvetica", 12)
        
        # KPIs (valores reais do dashboard)
        total_fila = request.args.get('total_fila', '0')
        total_servicos = request.args.get('total_servicos', '0')
        total_reincidencias = request.args.get('total_reincidencias', '0')
        tempo_medio_minutos = request.args.get('tempo_medio_minutos', '0')
        
        y_position -= 30
        p.drawString(100, y_position, f"Veículos na fila: {total_fila}")
        y_position -= 20
        p.drawString(100, y_position, f"Serviços em aberto: {total_servicos}")
        y_position -= 20
        p.drawString(100, y_position, f"Reincidências: {total_reincidencias}")
        y_position -= 20
        p.drawString(100, y_position, f"Tempo médio de atendimento: {tempo_medio_minutos} minutos")
        
        # Seção de detalhes
        y_position -= 40
        p.setFont("Helvetica-Bold", 14)
        p.drawString(100, y_position, "Detalhes por Filial")
        p.setFont("Helvetica", 12)
        
        # Detalhes por filial
        filial_selecionada = request.args.get('filial')
        for filial in FILIAIS:
            if filial_selecionada and filial != filial_selecionada:
                continue
                
            y_position -= 20
            if y_position < 100:
                p.showPage()
                y_position = height - 50
                p.setFont("Helvetica-Bold", 14)
                p.drawString(100, y_position, f"Detalhes por Filial (continuação)")
                p.setFont("Helvetica", 12)
                y_position -= 30
            
            # Contar veículos na fila da filial
            fila_filial = obter_fila(filial)
            count_fila = len(fila_filial)
            
            # Contar atendimentos no período
            count_atendimentos = 0
            for r in carregar_historico_local():
                if r.get('filial') == filial:
                    try:
                        data_liberacao = datetime.fromisoformat(r['liberado_em'])
                        data_limite = datetime.now() - timedelta(days=int(request.args.get('periodo', '7')))
                        if data_liberacao >= data_limite:
                            count_atendimentos += 1
                    except:
                        pass
            
            p.drawString(100, y_position, f"Filial {filial}: {count_fila} na fila, {count_atendimentos} atendidos")
        
        # Finaliza o PDF
        p.showPage()
        p.save()
        
        buffer.seek(0)
        return Response(
            buffer,
            mimetype="application/pdf",
            headers={"Content-Disposition": "attachment;filename=relatorio_atendimentos.pdf"}
        )
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        flash(f'Erro ao gerar relatório PDF: {str(e)}', 'danger')
        return redirect(url_for('dashboard'))

    
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT',5000)), debug=True)