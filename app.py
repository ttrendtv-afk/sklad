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
    role = db.Column(db.String(20), default='worker')
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
    state = db.Column(db.String(20), default='НОВАЯ')  # НОВАЯ / REF. / Б/У
    cost_usd = db.Column(db.Float, default=0)
    markup_pct = db.Column(db.Float, default=20)
    gel_rate = db.Column(db.Float, default=2.7)
    price_gel = db.Column(db.Float, default=0)  # final sell price GEL
    date_in = db.Column(db.Date, default=date.today)
    date_sold = db.Column(db.Date)
    date_returned = db.Column(db.Date)
    notes = db.Column(db.String(300))
    status = db.Column(db.String(20), default='на складе')  # на складе / продан / возврат
    added_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    sold_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    buyer = db.Column(db.String(100))

    model = db.relationship('PhoneModel', backref='phones')
    store = db.relationship('Store', backref='phones')

class Setting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True)
    value = db.Column(db.String(200))

# ─── AUTH ─────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*a, **kw)
    return dec

def admin_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            flash('Нет доступа', 'error')
            return redirect(url_for('catalog'))
        return f(*a, **kw)
    return dec

def get_setting(key, default=''):
    s = Setting.query.filter_by(key=key).first()
    return s.value if s else default

def set_setting(key, value):
    s = Setting.query.filter_by(key=key).first()
    if s:
        s.value = value
    else:
        db.session.add(Setting(key=key, value=value))
    db.session.commit()

# ─── ROUTES: AUTH ─────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = User.query.filter_by(username=request.form['username']).first()
        if u and check_password_hash(u.password_hash, request.form['password']):
            session.update({'user_id': u.id, 'username': u.username, 'role': u.role, 'name': u.name or u.username})
            return redirect(url_for('catalog'))
        flash('Неверный логин или пароль', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ─── ROUTES: CATALOG ──────────────────────────────────────────────────────────

@app.route('/')
@login_required
def catalog():
    search_imei = request.args.get('imei', '')
    sel_model = request.args.get('model', '')
    sel_store = request.args.get('store', '')
    sel_state = request.args.get('state', '')

    q = Phone.query.filter_by(status='на складе')
    if search_imei:
        q = q.filter(Phone.imei.ilike(f'%{search_imei}%'))
    if sel_model:
        q = q.filter(Phone.model_id == sel_model)
    if sel_store:
        q = q.filter(Phone.store_id == sel_store)
    if sel_state:
        q = q.filter(Phone.state == sel_state)

    phones = q.order_by(Phone.model_id, Phone.store_id).all()

    # Group by model
    from collections import defaultdict
    groups = defaultdict(lambda: {'phones': [], 'new': 0, 'ref': 0, 'bu': 0})
    for p in phones:
        key = p.model.name
        groups[key]['phones'].append(p)
        if p.state == 'НОВАЯ': groups[key]['new'] += 1
        elif p.state == 'REF.': groups[key]['ref'] += 1
        else: groups[key]['bu'] += 1

    models = PhoneModel.query.order_by(PhoneModel.name).all()
    stores = Store.query.order_by(Store.name).all()
    gel_rate = get_setting('gel_rate', '2.7')

    return render_template('catalog.html', groups=dict(groups), models=models, stores=stores,
                           search_imei=search_imei, sel_model=sel_model, sel_store=sel_store,
                           sel_state=sel_state, gel_rate=gel_rate)

@app.route('/phone/<imei>')
@login_required
def phone_detail(imei):
    p = Phone.query.filter_by(imei=imei).first_or_404()
    stores = Store.query.order_by(Store.name).all()
    return render_template('phone_detail.html', p=p, stores=stores)

@app.route('/phone/<imei>/edit', methods=['POST'])
@login_required
def phone_edit(imei):
    p = Phone.query.filter_by(imei=imei).first_or_404()
    p.state = request.form.get('state', p.state)
    p.store_id = int(request.form.get('store_id', p.store_id))
    p.price_gel = float(request.form.get('price_gel') or 0)
    p.cost_usd = float(request.form.get('cost_usd') or 0)
    p.markup_pct = float(request.form.get('markup_pct') or 20)
    p.gel_rate = float(request.form.get('gel_rate') or 2.7)
    p.notes = request.form.get('notes', '')

    di = request.form.get('date_in')
    if di:
        try: p.date_in = date.fromisoformat(di)
        except: pass

    ds = request.form.get('date_sold')
    p.date_sold = date.fromisoformat(ds) if ds else None

    dr = request.form.get('date_returned')
    p.date_returned = date.fromisoformat(dr) if dr else None

    if p.date_returned:
        p.status = 'возврат'
    elif p.date_sold:
        p.status = 'продан'
    else:
        p.status = 'на складе'

    db.session.commit()
    flash('Сохранено', 'success')
    return redirect(url_for('catalog'))

@app.route('/phone/<imei>/delete', methods=['POST'])
@admin_required
def phone_delete(imei):
    p = Phone.query.filter_by(imei=imei).first_or_404()
    db.session.delete(p)
    db.session.commit()
    flash('Товар удалён', 'success')
    return redirect(url_for('catalog'))

# ─── ROUTES: ADD PHONE ────────────────────────────────────────────────────────

@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_phone():
    models = PhoneModel.query.order_by(PhoneModel.name).all()
    stores = Store.query.order_by(Store.name).all()
    gel_rate = get_setting('gel_rate', '2.7')

    if request.method == 'POST':
        imei = request.form['imei'].strip()
        if Phone.query.filter_by(imei=imei).first():
            flash(f'IMEI {imei} уже существует!', 'error')
            return render_template('add.html', models=models, stores=stores, gel_rate=gel_rate, today=str(date.today()))

        cost_usd = float(request.form.get('cost_usd') or 0)
        markup_pct = float(request.form.get('markup_pct') or 20)
        gel_rate_val = float(request.form.get('gel_rate') or 2.7)
        price_gel = float(request.form.get('price_gel') or round(cost_usd * (1 + markup_pct / 100) * gel_rate_val, 2))

        p = Phone(
            model_id=int(request.form['model_id']),
            store_id=int(request.form['store_id']),
            imei=imei,
            state=request.form.get('state', 'НОВАЯ'),
            cost_usd=cost_usd,
            markup_pct=markup_pct,
            gel_rate=gel_rate_val,
            price_gel=price_gel,
            date_in=date.fromisoformat(request.form.get('date_in') or str(date.today())),
            notes=request.form.get('notes', ''),
            added_by=session['user_id'],
        )
        db.session.add(p)
        db.session.commit()
        flash(f'Товар IMEI {imei} добавлен', 'success')
        if request.form.get('another'):
            return redirect(url_for('add_phone'))
        return redirect(url_for('catalog'))

    return render_template('add.html', models=models, stores=stores, gel_rate=gel_rate, today=str(date.today()))

# ─── ROUTES: REPORTS ──────────────────────────────────────────────────────────

@app.route('/reports')
@login_required
def reports():
    tab = request.args.get('tab', 'sales')
    sel_store = request.args.get('store', '')
    sel_model = request.args.get('model', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    sel_state = request.args.get('state', '')
    sel_imei = request.args.get('imei', '')

    models = PhoneModel.query.order_by(PhoneModel.name).all()
    stores = Store.query.order_by(Store.name).all()

    def base_q(status_filter):
        q = Phone.query.filter_by(status=status_filter)
        if sel_store: q = q.filter(Phone.store_id == sel_store)
        if sel_model: q = q.filter(Phone.model_id == sel_model)
        if sel_state: q = q.filter(Phone.state == sel_state)
        if sel_imei: q = q.filter(Phone.imei.ilike(f'%{sel_imei}%'))
        return q

    def apply_date(q, field):
        if date_from:
            try: q = q.filter(field >= date.fromisoformat(date_from.replace('.', '-')))
            except: pass
        if date_to:
            try: q = q.filter(field <= date.fromisoformat(date_to.replace('.', '-')))
            except: pass
        return q

    phones = []
    if tab == 'sales':
        q = apply_date(base_q('продан'), Phone.date_sold)
        phones = q.order_by(Phone.date_sold.desc()).all()
    elif tab == 'stock':
        q = base_q('на складе')
        phones = q.order_by(Phone.date_in.desc()).all()
    elif tab == 'returns':
        q = apply_date(base_q('возврат'), Phone.date_returned)
        phones = q.order_by(Phone.date_returned.desc()).all()
    elif tab == 'finance':
        q = apply_date(base_q('продан'), Phone.date_sold)
        phones = q.order_by(Phone.date_sold.desc()).all()

    gel_rate = float(get_setting('gel_rate', '2.7'))

    return render_template('reports.html', phones=phones, tab=tab, models=models, stores=stores,
                           sel_store=sel_store, sel_model=sel_model, date_from=date_from,
                           date_to=date_to, sel_state=sel_state, sel_imei=sel_imei, gel_rate=gel_rate)

@app.route('/reports/export')
@login_required
def reports_export():
    tab = request.args.get('tab', 'sales')
    sel_store = request.args.get('store', '')
    sel_model = request.args.get('model', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    sel_state = request.args.get('state', '')

    status_map = {'sales': 'продан', 'stock': 'на складе', 'returns': 'возврат', 'finance': 'продан'}
    q = Phone.query.filter_by(status=status_map.get(tab, 'продан'))
    if sel_store: q = q.filter(Phone.store_id == sel_store)
    if sel_model: q = q.filter(Phone.model_id == sel_model)
    if sel_state: q = q.filter(Phone.state == sel_state)

    phones = q.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Модель', 'IMEI', 'Магазин', 'Состояние', 'Себестоимость $', 'Наценка %', 'Курс GEL', 'Цена GEL', 'Прибыль GEL', 'Дата приёма', 'Дата продажи', 'Дата возврата', 'Заметки'])
    for p in phones:
        profit = round(p.price_gel - p.cost_usd * p.gel_rate, 2) if p.price_gel and p.cost_usd else 0
        writer.writerow([p.model.name, p.imei, p.store.name, p.state, p.cost_usd, p.markup_pct, p.gel_rate, p.price_gel, profit, p.date_in, p.date_sold, p.date_returned, p.notes])

    output.seek(0)
    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment;filename=report_{tab}.csv'})

# ─── ROUTES: BULK MARKUP ──────────────────────────────────────────────────────

@app.route('/bulk-markup', methods=['POST'])
@login_required
def bulk_markup():
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
@login_required
def update_rate():
    rate = request.form.get('gel_rate', '2.7')
    set_setting('gel_rate', rate)
    flash(f'Курс GEL обновлён: {rate}', 'success')
    return redirect(request.referrer or url_for('catalog'))

# ─── ROUTES: REFERENCES ───────────────────────────────────────────────────────

@app.route('/references')
@admin_required
def references():
    models = PhoneModel.query.order_by(PhoneModel.name).all()
    stores = Store.query.order_by(Store.name).all()
    users = User.query.all()
    return render_template('references.html', models=models, stores=stores, users=users)

@app.route('/references/model/add', methods=['POST'])
@admin_required
def add_model():
    name = request.form['name'].strip()
    if name and not PhoneModel.query.filter_by(name=name).first():
        db.session.add(PhoneModel(name=name))
        db.session.commit()
        flash(f'Модель «{name}» добавлена', 'success')
    return redirect(url_for('references'))

@app.route('/references/model/delete/<int:mid>', methods=['POST'])
@admin_required
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
@admin_required
def add_store():
    name = request.form['name'].strip().upper()
    if name and not Store.query.filter_by(name=name).first():
        db.session.add(Store(name=name))
        db.session.commit()
        flash(f'Магазин «{name}» добавлен', 'success')
    return redirect(url_for('references'))

@app.route('/references/store/delete/<int:sid>', methods=['POST'])
@admin_required
def delete_store(sid):
    s = Store.query.get_or_404(sid)
    if s.phones:
        flash('Нельзя удалить — есть товары', 'error')
    else:
        db.session.delete(s)
        db.session.commit()
        flash('Магазин удалён', 'success')
    return redirect(url_for('references'))

@app.route('/references/user/add', methods=['POST'])
@admin_required
def add_user():
    username = request.form['username'].strip()
    password = request.form['password']
    name = request.form.get('name', '')
    role = request.form.get('role', 'worker')
    if User.query.filter_by(username=username).first():
        flash('Пользователь уже существует', 'error')
        return redirect(url_for('references'))
    db.session.add(User(username=username, password_hash=generate_password_hash(password), name=name, role=role))
    db.session.commit()
    flash(f'Пользователь {username} создан', 'success')
    return redirect(url_for('references'))

@app.route('/references/user/delete/<int:uid>', methods=['POST'])
@admin_required
def delete_user(uid):
    if uid == session['user_id']:
        flash('Нельзя удалить себя', 'error')
        return redirect(url_for('references'))
    u = User.query.get_or_404(uid)
    db.session.delete(u)
    db.session.commit()
    flash('Пользователь удалён', 'success')
    return redirect(url_for('references'))

# ─── BACKUP ───────────────────────────────────────────────────────────────────

@app.route('/backup/export')
@admin_required
def backup_export():
    phones = Phone.query.all()
    data = []
    for p in phones:
        data.append({
            'model': p.model.name, 'store': p.store.name, 'imei': p.imei,
            'state': p.state, 'cost_usd': p.cost_usd, 'markup_pct': p.markup_pct,
            'gel_rate': p.gel_rate, 'price_gel': p.price_gel,
            'date_in': str(p.date_in) if p.date_in else None,
            'date_sold': str(p.date_sold) if p.date_sold else None,
            'date_returned': str(p.date_returned) if p.date_returned else None,
            'status': p.status, 'notes': p.notes, 'buyer': p.buyer,
        })
    return Response(json.dumps({'phones': data, 'models': [m.name for m in PhoneModel.query.all()],
                                'stores': [s.name for s in Store.query.all()]}, ensure_ascii=False, indent=2),
                    mimetype='application/json',
                    headers={'Content-Disposition': 'attachment;filename=backup_wms.json'})

@app.route('/backup/import', methods=['POST'])
@admin_required
def backup_import():
    f = request.files.get('file')
    if not f:
        flash('Файл не выбран', 'error')
        return redirect(url_for('references'))
    try:
        data = json.load(f)
        # Import models
        for mname in data.get('models', []):
            if not PhoneModel.query.filter_by(name=mname).first():
                db.session.add(PhoneModel(name=mname))
        # Import stores
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
            if not m or not s:
                continue
            p = Phone(
                model_id=m.id, store_id=s.id, imei=row['imei'],
                state=row.get('state', 'НОВАЯ'),
                cost_usd=row.get('cost_usd', 0), markup_pct=row.get('markup_pct', 20),
                gel_rate=row.get('gel_rate', 2.7), price_gel=row.get('price_gel', 0),
                date_in=date.fromisoformat(row['date_in']) if row.get('date_in') else None,
                date_sold=date.fromisoformat(row['date_sold']) if row.get('date_sold') else None,
                date_returned=date.fromisoformat(row['date_returned']) if row.get('date_returned') else None,
                status=row.get('status', 'на складе'),
                notes=row.get('notes', ''), buyer=row.get('buyer', ''),
            )
            db.session.add(p)
            count += 1
        db.session.commit()
        flash(f'Импортировано {count} позиций', 'success')
    except Exception as e:
        flash(f'Ошибка импорта: {e}', 'error')
    return redirect(url_for('references'))

# ─── API ──────────────────────────────────────────────────────────────────────

@app.route('/api/phone/<imei>')
@login_required
def api_phone(imei):
    p = Phone.query.filter_by(imei=imei).first_or_404()
    stores = Store.query.all()
    return jsonify({
        'imei': p.imei, 'model': p.model.name, 'store': p.store.name,
        'store_id': p.store_id, 'state': p.state,
        'cost_usd': p.cost_usd, 'markup_pct': p.markup_pct,
        'gel_rate': p.gel_rate, 'price_gel': p.price_gel,
        'date_in': str(p.date_in) if p.date_in else '',
        'date_sold': str(p.date_sold) if p.date_sold else '',
        'date_returned': str(p.date_returned) if p.date_returned else '',
        'notes': p.notes or '', 'status': p.status,
        'stores': [{'id': s.id, 'name': s.name} for s in stores],
    })

@app.route('/api/calc-price')
@login_required
def api_calc_price():
    cost = float(request.args.get('cost', 0))
    markup = float(request.args.get('markup', 20))
    rate = float(request.args.get('rate', 2.7))
    price = round(cost * (1 + markup / 100) * rate, 2)
    return jsonify({'price': price})

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
    db.session.commit()

with app.app_context():
    init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
