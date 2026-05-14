from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, Response
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date
from functools import wraps
import os, json, csv, io

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'wms-secret-2024')
db_url = os.environ.get('DATABASE_URL', 'sqlite:///wms.db')
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ─── MODELS ───────────────────────────────────────────────────────────────────

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='worker')  # admin / manager / worker
    name = db.Column(db.String(100))

class Store(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)

class PhoneModel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), unique=True, nullable=False)

class Phone(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    model_id = db.Column(db.Integer, db.ForeignKey('phone_model.id'), nullable=False)
    store_id = db.Column(db.Integer, db.ForeignKey('store.id'), nullable=False)
    imei = db.Column(db.String(20), unique=True, nullable=False)
    state = db.Column(db.String(20), default='НОВАЯ')
    cost_usd = db.Column(db.Float, default=0)
    markup_pct = db.Column(db.Float, default=20)
    gel_rate = db.Column(db.Float, default=2.7)
    price_gel = db.Column(db.Float, default=0)      # listed price
    sold_price_gel = db.Column(db.Float)             # actual sold price (may differ)
    date_in = db.Column(db.Date, default=date.today)
    date_sold = db.Column(db.Date)
    date_returned = db.Column(db.Date)
    notes = db.Column(db.String(300))
    status = db.Column(db.String(20), default='на складе')
    added_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    buyer = db.Column(db.String(100))

    model = db.relationship('PhoneModel', backref='phones')
    store = db.relationship('Store', backref='phones')

class Setting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True)
    value = db.Column(db.String(200))

class PhoneState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    sort = db.Column(db.Integer, default=0)

# ─── ROLES ────────────────────────────────────────────────────────────────────
# admin   → everything
# manager → catalog, add, reports (sales/stock/returns), NO finance, NO references
# worker  → catalog (read-only), stock report only

ROLE_WEIGHTS = {'admin': 3, 'manager': 2, 'worker': 1}

def role_required(min_role):
    def decorator(f):
        @wraps(f)
        def dec(*a, **kw):
            if 'user_id' not in session:
                return redirect(url_for('login'))
            if ROLE_WEIGHTS.get(session.get('role'), 0) < ROLE_WEIGHTS.get(min_role, 99):
                flash('Нет доступа', 'error')
                return redirect(url_for('catalog'))
            return f(*a, **kw)
        return dec
    return decorator

login_required = role_required('worker')

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def get_setting(key, default=''):
    s = Setting.query.filter_by(key=key).first()
    return s.value if s else default

def set_setting(key, value):
    s = Setting.query.filter_by(key=key).first()
    if s: s.value = value
    else: db.session.add(Setting(key=key, value=value))
    db.session.commit()

def phone_to_dict(p):
    eff_sold = p.sold_price_gel if p.sold_price_gel is not None else p.price_gel
    profit = round((eff_sold or 0) - (p.cost_usd or 0) * (p.gel_rate or 2.7), 2)
    return {
        'id': p.id, 'imei': p.imei, 'model': p.model.name, 'model_id': p.model_id,
        'store': p.store.name, 'store_id': p.store_id, 'state': p.state,
        'cost_usd': p.cost_usd or 0, 'markup_pct': p.markup_pct or 20,
        'gel_rate': p.gel_rate or 2.7, 'price_gel': p.price_gel or 0,
        'sold_price_gel': p.sold_price_gel,
        'date_in': str(p.date_in) if p.date_in else '',
        'date_sold': str(p.date_sold) if p.date_sold else '',
        'date_returned': str(p.date_returned) if p.date_returned else '',
        'notes': p.notes or '', 'status': p.status, 'buyer': p.buyer or '',
        'profit': profit,
    }

def filter_phones(status=None):
    sel_store = request.args.get('store', '')
    sel_model = request.args.get('model', '')
    sel_state = request.args.get('state', '')
    sel_imei = request.args.get('imei', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    q = Phone.query
    if status: q = q.filter_by(status=status)
    if sel_store: q = q.filter(Phone.store_id == sel_store)
    if sel_model: q = q.filter(Phone.model_id == sel_model)
    if sel_state: q = q.filter(Phone.state == sel_state)
    if sel_imei: q = q.filter(Phone.imei.ilike(f'%{sel_imei}%'))
    if date_from:
        try: q = q.filter(Phone.date_in >= date.fromisoformat(date_from))
        except: pass
    if date_to:
        try: q = q.filter(Phone.date_in <= date.fromisoformat(date_to))
        except: pass
    return q

# ─── AUTH ─────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = User.query.filter_by(username=request.form['username']).first()
        if u and check_password_hash(u.password_hash, request.form['password']):
            session.update({'user_id': u.id, 'username': u.username,
                            'role': u.role, 'name': u.name or u.username})
            return redirect(url_for('catalog'))
        flash('Неверный логин или пароль', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ─── CATALOG ──────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def catalog():
    models = PhoneModel.query.order_by(PhoneModel.name).all()
    stores = Store.query.order_by(Store.name).all()
    gel_rate = get_setting('gel_rate', '2.7')
    return render_template('catalog.html', models=models, stores=stores, gel_rate=gel_rate)

@app.route('/api/catalog')
@login_required
def api_catalog():
    q = filter_phones(status='на складе')
    phones = q.order_by(Phone.model_id, Phone.store_id).all()
    from collections import defaultdict
    groups = defaultdict(lambda: {'model_id': 0, 'model': '', 'count': 0, 'new': 0, 'ref': 0, 'bu': 0, 'phones': []})
    for p in phones:
        k = p.model.name
        groups[k]['model_id'] = p.model_id
        groups[k]['model'] = k
        groups[k]['count'] += 1
        if p.state == 'НОВАЯ': groups[k]['new'] += 1
        elif p.state == 'REF.': groups[k]['ref'] += 1
        else: groups[k]['bu'] += 1
        groups[k]['phones'].append(phone_to_dict(p))
    return jsonify(list(groups.values()))

@app.route('/api/model-phones/<int:model_id>')
@login_required
def api_model_phones(model_id):
    sel_store = request.args.get('store', '')
    sel_state = request.args.get('state', '')
    sel_imei = request.args.get('imei', '')
    q = Phone.query.filter_by(model_id=model_id, status='на складе')
    if sel_store: q = q.filter(Phone.store_id == sel_store)
    if sel_state: q = q.filter(Phone.state == sel_state)
    if sel_imei: q = q.filter(Phone.imei.ilike(f'%{sel_imei}%'))
    phones = q.all()
    stores = Store.query.all()
    return jsonify({'phones': [phone_to_dict(p) for p in phones],
                    'stores': [{'id': s.id, 'name': s.name} for s in stores]})

@app.route('/api/phone/<imei>')
@login_required
def api_phone(imei):
    p = Phone.query.filter_by(imei=imei).first_or_404()
    stores = Store.query.all()
    d = phone_to_dict(p)
    d['stores'] = [{'id': s.id, 'name': s.name} for s in stores]
    return jsonify(d)

@app.route('/phone/<imei>/edit', methods=['POST'])
@role_required('manager')
def phone_edit(imei):
    p = Phone.query.filter_by(imei=imei).first_or_404()
    p.state = request.form.get('state', p.state)
    p.store_id = int(request.form.get('store_id', p.store_id))
    p.cost_usd = float(request.form.get('cost_usd') or 0)
    p.markup_pct = float(request.form.get('markup_pct') or 20)
    p.gel_rate = float(request.form.get('gel_rate') or 2.7)
    p.price_gel = float(request.form.get('price_gel') or 0)
    sp = request.form.get('sold_price_gel')
    p.sold_price_gel = float(sp) if sp else None
    p.notes = request.form.get('notes', '')
    p.buyer = request.form.get('buyer', '')

    di = request.form.get('date_in')
    if di:
        try: p.date_in = date.fromisoformat(di)
        except: pass
    ds = request.form.get('date_sold')
    p.date_sold = date.fromisoformat(ds) if ds else None
    dr = request.form.get('date_returned')
    p.date_returned = date.fromisoformat(dr) if dr else None

    if p.date_returned: p.status = 'возврат'
    elif p.date_sold: p.status = 'продан'
    else: p.status = 'на складе'

    db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})
    flash('Сохранено', 'success')
    return redirect(url_for('catalog'))

@app.route('/phone/<imei>/delete', methods=['POST'])
@role_required('admin')
def phone_delete(imei):
    p = Phone.query.filter_by(imei=imei).first_or_404()
    db.session.delete(p)
    db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})
    flash('Удалено', 'success')
    return redirect(url_for('catalog'))

# ─── ADD PHONE ────────────────────────────────────────────────────────────────

@app.route('/add', methods=['GET', 'POST'])
@role_required('manager')
def add_phone():
    models = PhoneModel.query.order_by(PhoneModel.name).all()
    stores = Store.query.order_by(Store.name).all()
    gel_rate = get_setting('gel_rate', '2.7')
    states = [s.name for s in PhoneState.query.order_by(PhoneState.sort).all()] or ['НОВАЯ','REF.','Б/У']

    if request.method == 'POST':
        imei = request.form['imei'].strip()
        if Phone.query.filter_by(imei=imei).first():
            flash(f'IMEI {imei} уже существует!', 'error')
            return render_template('add.html', models=models, stores=stores, gel_rate=gel_rate, today=str(date.today()), states=states)

        cost_usd = float(request.form.get('cost_usd') or 0)
        markup_pct = float(request.form.get('markup_pct') or 20)
        gel_rate_val = float(request.form.get('gel_rate') or 2.7)
        price_gel = float(request.form.get('price_gel') or round(cost_usd * (1 + markup_pct / 100) * gel_rate_val, 2))

        p = Phone(
            model_id=int(request.form['model_id']),
            store_id=int(request.form['store_id']),
            imei=imei, state=request.form.get('state', states[0] if states else 'НОВАЯ'),
            cost_usd=cost_usd, markup_pct=markup_pct,
            gel_rate=gel_rate_val, price_gel=price_gel,
            date_in=date.fromisoformat(request.form.get('date_in') or str(date.today())),
            notes=request.form.get('notes', ''), added_by=session['user_id'],
        )
        db.session.add(p)
        db.session.commit()
        flash(f'Товар IMEI {imei} добавлен', 'success')
        if request.form.get('another'):
            return redirect(url_for('add_phone'))
        return redirect(url_for('catalog'))

    return render_template('add.html', models=models, stores=stores, gel_rate=gel_rate, today=str(date.today()), states=states)

# ─── IMPORT FROM EXCEL/CSV ────────────────────────────────────────────────────

@app.route('/import', methods=['GET', 'POST'])
@role_required('manager')
def import_phones():
    models = PhoneModel.query.order_by(PhoneModel.name).all()
    stores = Store.query.order_by(Store.name).all()
    gel_rate = get_setting('gel_rate', '2.7')

    if request.method == 'POST':
        f = request.files.get('file')
        if not f:
            flash('Файл не выбран', 'error')
            return redirect(url_for('import_phones'))

        model_id = int(request.form['model_id'])
        store_id = int(request.form['store_id'])
        state = request.form.get('state', 'НОВАЯ')
        cost_usd = float(request.form.get('cost_usd') or 0)
        markup_pct = float(request.form.get('markup_pct') or 20)
        gel_rate_val = float(request.form.get('gel_rate') or 2.7)
        date_in_str = request.form.get('date_in') or str(date.today())
        price_gel = round(cost_usd * (1 + markup_pct / 100) * gel_rate_val, 2)

        content = f.read().decode('utf-8-sig', errors='replace')
        reader = csv.reader(io.StringIO(content))
        added, skipped = 0, 0
        for row in reader:
            for cell in row:
                imei = cell.strip().replace(' ', '').replace('\t', '')
                if not imei or not imei.isdigit(): continue
                if Phone.query.filter_by(imei=imei).first():
                    skipped += 1
                    continue
                p = Phone(
                    model_id=model_id, store_id=store_id, imei=imei,
                    state=state, cost_usd=cost_usd, markup_pct=markup_pct,
                    gel_rate=gel_rate_val, price_gel=price_gel,
                    date_in=date.fromisoformat(date_in_str),
                    added_by=session['user_id'],
                )
                db.session.add(p)
                added += 1
        db.session.commit()
        flash(f'Импортировано: {added} шт. Пропущено (дубли): {skipped} шт.', 'success')
        return redirect(url_for('catalog'))

    return render_template('import.html', models=models, stores=stores, gel_rate=gel_rate, today=str(date.today()))

@app.route('/import/template')
@login_required
def import_template():
    content = "imei\n351111111111111\n351111111111222\n"
    return Response(content, mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment;filename=imei_template.csv'})

# ─── FINANCE (admin/manager only) ────────────────────────────────────────────

@app.route('/finance')
@role_required('manager')
def finance():
    models = PhoneModel.query.order_by(PhoneModel.name).all()
    stores = Store.query.order_by(Store.name).all()
    gel_rate = float(get_setting('gel_rate', '2.7'))
    sel_store = request.args.get('store', '')
    sel_model = request.args.get('model', '')
    sel_state = request.args.get('state', '')
    sel_imei = request.args.get('imei', '')
    states = [s.name for s in PhoneState.query.order_by(PhoneState.sort).all()] or ['НОВАЯ','REF.','Б/У']
    return render_template('finance.html', models=models, stores=stores, gel_rate=gel_rate,
                           sel_store=sel_store, sel_model=sel_model, sel_state=sel_state,
                           sel_imei=sel_imei, states=states)

@app.route('/api/finance')
@role_required('manager')
def api_finance():
    sel_store = request.args.get('store', '')
    sel_model = request.args.get('model', '')
    sel_state = request.args.get('state', '')
    sel_imei = request.args.get('imei', '')
    gel_rate = float(get_setting('gel_rate', '2.7'))
    q = Phone.query.filter_by(status='на складе')
    if sel_store: q = q.filter(Phone.store_id == sel_store)
    if sel_model: q = q.filter(Phone.model_id == sel_model)
    if sel_state: q = q.filter(Phone.state == sel_state)
    if sel_imei: q = q.filter(Phone.imei.ilike(f'%{sel_imei}%'))
    phones = q.order_by(Phone.model_id).all()
    result = []
    total_price = total_profit = 0
    for p in phones:
        profit = round((p.price_gel or 0) - (p.cost_usd or 0) * (p.gel_rate or gel_rate), 2)
        total_price += p.price_gel or 0
        total_profit += profit
        result.append(phone_to_dict(p))
    return jsonify({'phones': result, 'count': len(result),
                    'total_price': round(total_price, 2),
                    'total_profit': round(total_profit, 2)})

@app.route('/finance/bulk', methods=['POST'])
@role_required('manager')
def finance_bulk():
    markup = float(request.form.get('markup', 20))
    gel_rate = float(request.form.get('gel_rate', 2.7))
    sel_store = request.form.get('store', '')
    sel_model = request.form.get('model', '')
    sel_state = request.form.get('state', '')
    q = Phone.query.filter_by(status='на складе')
    if sel_store: q = q.filter(Phone.store_id == sel_store)
    if sel_model: q = q.filter(Phone.model_id == sel_model)
    if sel_state: q = q.filter(Phone.state == sel_state)
    phones = q.all()
    for p in phones:
        if p.cost_usd:
            p.markup_pct = markup
            p.gel_rate = gel_rate
            p.price_gel = round(p.cost_usd * (1 + markup / 100) * gel_rate, 2)
    set_setting('gel_rate', str(gel_rate))
    db.session.commit()
    flash(f'Обновлено {len(phones)} позиций', 'success')
    return redirect(url_for('finance', store=sel_store, model=sel_model, state=sel_state))



@app.route('/reports')
@role_required('worker')
def reports():
    tab = request.args.get('tab', 'sales')
    allowed = ['sales', 'returns']
    if tab not in allowed:
        tab = 'sales'

    models = PhoneModel.query.order_by(PhoneModel.name).all()
    stores = Store.query.order_by(Store.name).all()
    gel_rate = float(get_setting('gel_rate', '2.7'))
    can_finance = session.get('role') in ('admin', 'manager')

    sel_store = request.args.get('store', '')
    sel_model = request.args.get('model', '')
    sel_state = request.args.get('state', '')
    sel_imei = request.args.get('imei', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    status_map = {'sales': 'продан', 'returns': 'возврат'}
    q = Phone.query.filter_by(status=status_map[tab])
    if sel_store: q = q.filter(Phone.store_id == sel_store)
    if sel_model: q = q.filter(Phone.model_id == sel_model)
    if sel_state: q = q.filter(Phone.state == sel_state)
    if sel_imei: q = q.filter(Phone.imei.ilike(f'%{sel_imei}%'))

    date_field = Phone.date_sold if tab == 'sales' else Phone.date_returned
    if date_from:
        try: q = q.filter(date_field >= date.fromisoformat(date_from))
        except: pass
    if date_to:
        try: q = q.filter(date_field <= date.fromisoformat(date_to))
        except: pass

    phones = q.order_by(date_field.desc()).all()

    return render_template('reports.html', phones=phones, tab=tab, models=models, stores=stores,
                           sel_store=sel_store, sel_model=sel_model, sel_state=sel_state,
                           sel_imei=sel_imei, date_from=date_from, date_to=date_to,
                           gel_rate=gel_rate, can_finance=can_finance)

@app.route('/reports/export')
@role_required('worker')
def reports_export():
    tab = request.args.get('tab', 'stock')
    sel_store = request.args.get('store', '')
    sel_model = request.args.get('model', '')
    sel_state = request.args.get('state', '')
    sel_imei = request.args.get('imei', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    gel_rate = float(get_setting('gel_rate', '2.7'))

    status_map = {'sales': 'продан', 'stock': 'на складе', 'returns': 'возврат', 'finance': 'продан'}
    q = Phone.query.filter_by(status=status_map.get(tab, 'на складе'))
    if sel_store: q = q.filter(Phone.store_id == sel_store)
    if sel_model: q = q.filter(Phone.model_id == sel_model)
    if sel_state: q = q.filter(Phone.state == sel_state)
    if sel_imei: q = q.filter(Phone.imei.ilike(f'%{sel_imei}%'))

    date_field = Phone.date_sold if tab in ('sales','finance') else (Phone.date_returned if tab=='returns' else Phone.date_in)
    if date_from:
        try: q = q.filter(date_field >= date.fromisoformat(date_from))
        except: pass
    if date_to:
        try: q = q.filter(date_field <= date.fromisoformat(date_to))
        except: pass

    phones = q.all()
    can_finance = session.get('role') in ('admin', 'manager')

    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    if can_finance:
        writer.writerow(['Модель','IMEI','Магазин','Состояние','Себестоимость $','Наценка %','Курс GEL','Цена GEL','Цена продажи GEL','Прибыль GEL','Дата приёма','Дата продажи','Дата возврата','Покупатель','Заметки'])
        for p in phones:
            eff = p.sold_price_gel if p.sold_price_gel is not None else p.price_gel
            profit = round((eff or 0) - (p.cost_usd or 0) * (p.gel_rate or gel_rate), 2)
            writer.writerow([p.model.name, p.imei, p.store.name, p.state, p.cost_usd, p.markup_pct, p.gel_rate, p.price_gel, eff, profit, p.date_in, p.date_sold, p.date_returned, p.buyer, p.notes])
    else:
        writer.writerow(['Модель','IMEI','Магазин','Состояние','Цена GEL','Дата приёма','Дата продажи','Заметки'])
        for p in phones:
            writer.writerow([p.model.name, p.imei, p.store.name, p.state, p.price_gel, p.date_in, p.date_sold, p.notes])

    output.seek(0)
    return Response('\ufeff' + output.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment;filename=report_{tab}.csv'})

@app.route('/api/reports')
@role_required('worker')
def api_reports():
    tab = request.args.get('tab', 'stock')
    sel_store = request.args.get('store', '')
    sel_model = request.args.get('model', '')
    sel_state = request.args.get('state', '')
    sel_imei = request.args.get('imei', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    gel_rate = float(get_setting('gel_rate', '2.7'))

    status_map = {'sales': 'продан', 'stock': 'на складе', 'returns': 'возврат', 'finance': 'продан'}
    q = Phone.query.filter_by(status=status_map.get(tab, 'на складе'))
    if sel_store: q = q.filter(Phone.store_id == sel_store)
    if sel_model: q = q.filter(Phone.model_id == sel_model)
    if sel_state: q = q.filter(Phone.state == sel_state)
    if sel_imei: q = q.filter(Phone.imei.ilike(f'%{sel_imei}%'))

    date_field = Phone.date_sold if tab in ('sales','finance') else (Phone.date_returned if tab=='returns' else Phone.date_in)
    if date_from:
        try: q = q.filter(date_field >= date.fromisoformat(date_from))
        except: pass
    if date_to:
        try: q = q.filter(date_field <= date.fromisoformat(date_to))
        except: pass

    phones = q.order_by(date_field.desc()).all()
    can_finance = session.get('role') in ('admin', 'manager')
    result = []
    total_revenue = total_profit = 0
    for p in phones:
        eff = p.sold_price_gel if p.sold_price_gel is not None else p.price_gel
        profit = round((eff or 0) - (p.cost_usd or 0) * (p.gel_rate or gel_rate), 2)
        total_revenue += eff or 0
        total_profit += profit
        row = phone_to_dict(p)
        row['effective_price'] = eff
        if not can_finance:
            del row['cost_usd'], row['markup_pct'], row['gel_rate'], row['profit']
        result.append(row)

    return jsonify({'phones': result, 'count': len(result),
                    'total_revenue': round(total_revenue, 2),
                    'total_profit': round(total_profit, 2),
                    'can_finance': can_finance})

# ─── BULK MARKUP ──────────────────────────────────────────────────────────────

@app.route('/finance/bulk', methods=['POST'])
@role_required('manager')
def finance_bulk():
    return bulk_markup_action()

@app.route('/bulk-markup', methods=['POST'])
@role_required('manager')
def bulk_markup():
    return bulk_markup_action()

def bulk_markup_action():
    markup = float(request.form.get('markup', 20))
    gel_rate = float(request.form.get('gel_rate', 2.7))
    sel_store = request.form.get('store', '')
    sel_model = request.form.get('model', '')
    sel_state = request.form.get('state', '')

    q = Phone.query.filter_by(status='на складе')
    if sel_store: q = q.filter(Phone.store_id == sel_store)
    if sel_model: q = q.filter(Phone.model_id == sel_model)
    if sel_state: q = q.filter(Phone.state == sel_state)

    phones = q.all()
    for p in phones:
        if p.cost_usd:
            p.markup_pct = markup
            p.gel_rate = gel_rate
            p.price_gel = round(p.cost_usd * (1 + markup / 100) * gel_rate, 2)

    set_setting('gel_rate', str(gel_rate))
    db.session.commit()
    flash(f'Обновлено {len(phones)} позиций', 'success')
    return redirect(url_for('reports', tab='finance', store=sel_store, model=sel_model, state=sel_state))

@app.route('/update-rate', methods=['POST'])
@role_required('manager')
def update_rate():
    rate = request.form.get('gel_rate', '2.7')
    set_setting('gel_rate', rate)
    flash(f'Курс GEL обновлён: {rate}', 'success')
    return redirect(request.referrer or url_for('catalog'))

# ─── REFERENCES ───────────────────────────────────────────────────────────────

@app.route('/references')
@role_required('admin')
def references():
    models = PhoneModel.query.order_by(PhoneModel.name).all()
    stores = Store.query.order_by(Store.name).all()
    users = User.query.all()
    states = PhoneState.query.order_by(PhoneState.sort).all()
    return render_template('references.html', models=models, stores=stores, users=users, states=states)

@app.route('/references/model/add', methods=['POST'])
@role_required('admin')
def add_model():
    name = request.form['name'].strip()
    if name and not PhoneModel.query.filter_by(name=name).first():
        db.session.add(PhoneModel(name=name))
        db.session.commit()
        flash(f'Модель «{name}» добавлена', 'success')
    return redirect(url_for('references'))

@app.route('/references/model/edit/<int:mid>', methods=['POST'])
@role_required('admin')
def edit_model(mid):
    m = PhoneModel.query.get_or_404(mid)
    m.name = request.form['name'].strip()
    db.session.commit()
    flash('Модель обновлена', 'success')
    return redirect(url_for('references'))

@app.route('/references/model/delete/<int:mid>', methods=['POST'])
@role_required('admin')
def delete_model(mid):
    m = PhoneModel.query.get_or_404(mid)
    if m.phones:
        flash('Нельзя удалить — есть товары', 'error')
    else:
        db.session.delete(m)
        db.session.commit()
        flash('Модель удалена', 'success')
    return redirect(url_for('references'))

@app.route('/references/store/add', methods=['POST'])
@role_required('admin')
def add_store():
    name = request.form['name'].strip().upper()
    if name and not Store.query.filter_by(name=name).first():
        db.session.add(Store(name=name))
        db.session.commit()
        flash(f'Магазин «{name}» добавлен', 'success')
    return redirect(url_for('references'))

@app.route('/references/store/delete/<int:sid>', methods=['POST'])
@role_required('admin')
def delete_store(sid):
    s = Store.query.get_or_404(sid)
    if s.phones:
        flash('Нельзя удалить — есть товары', 'error')
    else:
        db.session.delete(s)
        db.session.commit()
        flash('Магазин удалён', 'success')
    return redirect(url_for('references'))

@app.route('/references/state/add', methods=['POST'])
@role_required('admin')
def add_state():
    name = request.form['name'].strip()
    if name and not PhoneState.query.filter_by(name=name).first():
        count = PhoneState.query.count()
        db.session.add(PhoneState(name=name, sort=count))
        db.session.commit()
        flash(f'Состояние «{name}» добавлено', 'success')
    return redirect(url_for('references'))

@app.route('/references/state/delete/<int:sid>', methods=['POST'])
@role_required('admin')
def delete_state(sid):
    s = PhoneState.query.get_or_404(sid)
    db.session.delete(s)
    db.session.commit()
    flash('Состояние удалено', 'success')
    return redirect(url_for('references'))


@app.route('/references/user/add', methods=['POST'])
@role_required('admin')
def add_user():
    username = request.form['username'].strip()
    if User.query.filter_by(username=username).first():
        flash('Пользователь уже существует', 'error')
        return redirect(url_for('references'))
    db.session.add(User(username=username,
                        password_hash=generate_password_hash(request.form['password']),
                        name=request.form.get('name', ''),
                        role=request.form.get('role', 'worker')))
    db.session.commit()
    flash(f'Пользователь {username} создан', 'success')
    return redirect(url_for('references'))

@app.route('/references/user/delete/<int:uid>', methods=['POST'])
@role_required('admin')
def delete_user(uid):
    if uid == session['user_id']:
        flash('Нельзя удалить себя', 'error')
        return redirect(url_for('references'))
    u = User.query.get_or_404(uid)
    db.session.delete(u)
    db.session.commit()
    flash('Удалён', 'success')
    return redirect(url_for('references'))

# ─── BACKUP ───────────────────────────────────────────────────────────────────

@app.route('/backup/export')
@role_required('admin')
def backup_export():
    phones = Phone.query.all()
    data = [phone_to_dict(p) for p in phones]
    for d in data:
        d.pop('stores', None)
    out = json.dumps({'phones': data,
                      'models': [m.name for m in PhoneModel.query.all()],
                      'stores': [s.name for s in Store.query.all()]},
                     ensure_ascii=False, indent=2)
    return Response(out, mimetype='application/json',
                    headers={'Content-Disposition': 'attachment;filename=backup_wms.json'})

@app.route('/backup/import', methods=['POST'])
@role_required('admin')
def backup_import():
    f = request.files.get('file')
    if not f:
        flash('Файл не выбран', 'error')
        return redirect(url_for('references'))
    try:
        data = json.load(f)
        for mname in data.get('models', []):
            if not PhoneModel.query.filter_by(name=mname).first():
                db.session.add(PhoneModel(name=mname))
        for sname in data.get('stores', []):
            if not Store.query.filter_by(name=sname).first():
                db.session.add(Store(name=sname))
        db.session.flush()
        count = 0
        for row in data.get('phones', []):
            if Phone.query.filter_by(imei=row['imei']).first():
                continue
            m = PhoneModel.query.filter_by(name=row['model']).first()
            s = Store.query.filter_by(name=row['store']).first()
            if not m or not s: continue
            p = Phone(
                model_id=m.id, store_id=s.id, imei=row['imei'],
                state=row.get('state','НОВАЯ'), cost_usd=row.get('cost_usd',0),
                markup_pct=row.get('markup_pct',20), gel_rate=row.get('gel_rate',2.7),
                price_gel=row.get('price_gel',0), sold_price_gel=row.get('sold_price_gel'),
                date_in=date.fromisoformat(row['date_in']) if row.get('date_in') else None,
                date_sold=date.fromisoformat(row['date_sold']) if row.get('date_sold') else None,
                date_returned=date.fromisoformat(row['date_returned']) if row.get('date_returned') else None,
                status=row.get('status','на складе'), notes=row.get('notes',''), buyer=row.get('buyer',''),
            )
            db.session.add(p)
            count += 1
        db.session.commit()
        flash(f'Импортировано {count} позиций', 'success')
    except Exception as e:
        flash(f'Ошибка: {e}', 'error')
    return redirect(url_for('references'))

# ─── INIT ─────────────────────────────────────────────────────────────────────

def init_db():
    db.create_all()
    if not User.query.filter_by(username='admin').first():
        db.session.add(User(username='admin', password_hash=generate_password_hash('admin123'), role='admin', name='Администратор'))
    if not Store.query.filter_by(name='СКЛАД').first():
        for s in ['СКЛАД', 'МАГАЗИН 1', 'МАГАЗИН 2']:
            db.session.add(Store(name=s))
    if not Setting.query.filter_by(key='gel_rate').first():
        db.session.add(Setting(key='gel_rate', value='2.7'))
    if not PhoneState.query.first():
        for i, s in enumerate(['НОВАЯ', 'REF.', 'Б/У']):
            db.session.add(PhoneState(name=s, sort=i))
    if not PhoneModel.query.first():
        iphone_models = [
            'iPhone 13 128GB', 'iPhone 13 256GB',
            'iPhone 13 Pro 128GB', 'iPhone 13 Pro 256GB',
            'iPhone 14 128GB', 'iPhone 14 256GB',
            'iPhone 14 Pro 128GB', 'iPhone 14 Pro 256GB',
            'iPhone 14 Pro Max 256GB', 'iPhone 14 Pro Max 512GB',
            'iPhone 15 128GB', 'iPhone 15 256GB',
            'iPhone 15 Pro 128GB', 'iPhone 15 Pro 256GB',
            'iPhone 15 Pro 512GB', 'iPhone 15 Pro Max 256GB',
            'iPhone 16 128GB', 'iPhone 16 256GB',
            'iPhone 16 Pro 256GB', 'iPhone 16 Pro Max 256GB',
        ]
        for name in iphone_models:
            db.session.add(PhoneModel(name=name))
    db.session.commit()

with app.app_context():
    init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
