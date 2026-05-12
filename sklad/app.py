from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date
from functools import wraps
import os

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'sklad-secret-key-2024')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///sklad.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ─── MODELS ───────────────────────────────────────────────────────────────────

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='worker')  # admin / worker
    name = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Phone(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    brand = db.Column(db.String(50), nullable=False)
    model = db.Column(db.String(100), nullable=False)
    imei = db.Column(db.String(20))
    color = db.Column(db.String(50))
    storage = db.Column(db.String(20))
    condition = db.Column(db.String(50), default='Новый')
    cost_price = db.Column(db.Float, nullable=False)
    sell_price = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default='на складе')  # на складе / продан
    supplier = db.Column(db.String(100))
    notes = db.Column(db.String(300))
    added_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    added_date = db.Column(db.Date, default=date.today)
    sold_price = db.Column(db.Float)
    sold_date = db.Column(db.Date)
    sold_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    buyer = db.Column(db.String(100))

class HistoryLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, default=date.today)
    operation = db.Column(db.String(20))  # приход / продажа
    phone_id = db.Column(db.Integer, db.ForeignKey('phone.id'))
    model_name = db.Column(db.String(150))
    imei = db.Column(db.String(20))
    sum = db.Column(db.Float)
    profit = db.Column(db.Float)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    buyer = db.Column(db.String(100))

# ─── AUTH ─────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = User.query.get(session['user_id'])
        if user.role != 'admin':
            flash('Нет доступа', 'error')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated

# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            session['username'] = user.username
            session['role'] = user.role
            session['name'] = user.name or user.username
            return redirect(url_for('index'))
        flash('Неверный логин или пароль', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    stats = get_stats()
    return render_template('index.html', stats=stats)

@app.route('/stock')
@login_required
def stock():
    search = request.args.get('search', '')
    brand = request.args.get('brand', '')
    status = request.args.get('status', '')
    q = Phone.query
    if search:
        q = q.filter(
            db.or_(Phone.model.ilike(f'%{search}%'),
                   Phone.brand.ilike(f'%{search}%'),
                   Phone.imei.ilike(f'%{search}%'),
                   Phone.color.ilike(f'%{search}%'))
        )
    if brand:
        q = q.filter_by(brand=brand)
    if status:
        q = q.filter_by(status=status)
    phones = q.order_by(Phone.id.desc()).all()
    brands = db.session.query(Phone.brand).distinct().all()
    brands = [b[0] for b in brands]
    return render_template('stock.html', phones=phones, brands=brands, search=search, sel_brand=brand, sel_status=status)

@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_phone():
    if request.method == 'POST':
        phone = Phone(
            brand=request.form['brand'],
            model=request.form['model'],
            imei=request.form.get('imei',''),
            color=request.form.get('color',''),
            storage=request.form.get('storage',''),
            condition=request.form.get('condition','Новый'),
            cost_price=float(request.form['cost_price']),
            sell_price=float(request.form['sell_price']),
            supplier=request.form.get('supplier',''),
            notes=request.form.get('notes',''),
            added_by=session['user_id'],
            added_date=date.fromisoformat(request.form.get('added_date', str(date.today()))),
        )
        db.session.add(phone)
        db.session.flush()
        log = HistoryLog(
            operation='приход',
            phone_id=phone.id,
            model_name=f"{phone.brand} {phone.model}",
            imei=phone.imei,
            sum=phone.cost_price,
            user_id=session['user_id'],
            buyer=phone.supplier or '—',
        )
        db.session.add(log)
        db.session.commit()
        flash(f'✅ {phone.brand} {phone.model} добавлен на склад', 'success')
        return redirect(url_for('stock'))
    return render_template('add.html', today=str(date.today()))

@app.route('/sell/<int:phone_id>', methods=['GET', 'POST'])
@login_required
def sell_phone(phone_id):
    phone = Phone.query.get_or_404(phone_id)
    if phone.status == 'продан':
        flash('Этот телефон уже продан', 'error')
        return redirect(url_for('stock'))
    if request.method == 'POST':
        sold_price = float(request.form['sold_price'])
        buyer = request.form.get('buyer', '')
        sold_date = date.fromisoformat(request.form.get('sold_date', str(date.today())))
        phone.status = 'продан'
        phone.sold_price = sold_price
        phone.sold_date = sold_date
        phone.sold_by = session['user_id']
        phone.buyer = buyer
        profit = sold_price - phone.cost_price
        log = HistoryLog(
            operation='продажа',
            phone_id=phone.id,
            model_name=f"{phone.brand} {phone.model}",
            imei=phone.imei,
            sum=sold_price,
            profit=profit,
            user_id=session['user_id'],
            buyer=buyer or '—',
        )
        db.session.add(log)
        db.session.commit()
        flash(f'💸 {phone.brand} {phone.model} продан за {sold_price:,.0f} ₽ | Прибыль: {profit:,.0f} ₽', 'success')
        return redirect(url_for('stock'))
    return render_template('sell.html', phone=phone, today=str(date.today()))

@app.route('/history')
@login_required
def history():
    logs = HistoryLog.query.order_by(HistoryLog.id.desc()).limit(200).all()
    users = {u.id: u.name or u.username for u in User.query.all()}
    return render_template('history.html', logs=logs, users=users)

@app.route('/reports')
@login_required
def reports():
    stats = get_stats()
    # Top brands sold
    from sqlalchemy import func
    brand_sales = db.session.query(Phone.brand, func.count(Phone.id).label('cnt'))\
        .filter_by(status='продан').group_by(Phone.brand).order_by(func.count(Phone.id).desc()).all()
    brand_stock = db.session.query(Phone.brand, func.count(Phone.id).label('cnt'))\
        .filter_by(status='на складе').group_by(Phone.brand).order_by(func.count(Phone.id).desc()).all()
    # Top models by profit
    top_models = db.session.query(
        Phone.brand, Phone.model,
        func.count(Phone.id).label('cnt'),
        func.sum(Phone.sold_price - Phone.cost_price).label('profit')
    ).filter_by(status='продан').group_by(Phone.brand, Phone.model)\
     .order_by(func.sum(Phone.sold_price - Phone.cost_price).desc()).limit(10).all()
    return render_template('reports.html', stats=stats, brand_sales=brand_sales, brand_stock=brand_stock, top_models=top_models)

@app.route('/users')
@admin_required
def users():
    all_users = User.query.all()
    return render_template('users.html', users=all_users)

@app.route('/users/add', methods=['POST'])
@admin_required
def add_user():
    username = request.form['username']
    password = request.form['password']
    name = request.form.get('name', '')
    role = request.form.get('role', 'worker')
    if User.query.filter_by(username=username).first():
        flash('Пользователь уже существует', 'error')
        return redirect(url_for('users'))
    u = User(username=username, password_hash=generate_password_hash(password), name=name, role=role)
    db.session.add(u)
    db.session.commit()
    flash(f'✅ Пользователь {username} создан', 'success')
    return redirect(url_for('users'))

@app.route('/users/delete/<int:uid>', methods=['POST'])
@admin_required
def delete_user(uid):
    if uid == session['user_id']:
        flash('Нельзя удалить себя', 'error')
        return redirect(url_for('users'))
    u = User.query.get_or_404(uid)
    db.session.delete(u)
    db.session.commit()
    flash('Пользователь удалён', 'success')
    return redirect(url_for('users'))

def get_stats():
    sold = Phone.query.filter_by(status='продан').all()
    in_stock = Phone.query.filter_by(status='на складе').count()
    total_revenue = sum(p.sold_price or 0 for p in sold)
    total_cost_sold = sum(p.cost_price for p in sold)
    profit = total_revenue - total_cost_sold
    stock_value = db.session.query(db.func.sum(Phone.cost_price)).filter_by(status='на складе').scalar() or 0
    brands = db.session.query(Phone.brand).distinct().count()
    return dict(in_stock=in_stock, sold=len(sold), profit=profit,
                revenue=total_revenue, stock_value=stock_value, brands=brands)

# ─── INIT ─────────────────────────────────────────────────────────────────────

def create_default_admin():
    if not User.query.filter_by(username='admin').first():
        admin = User(
            username='admin',
            password_hash=generate_password_hash('admin123'),
            role='admin',
            name='Администратор'
        )
        db.session.add(admin)
        db.session.commit()
        print("✅ Создан admin / admin123")

with app.app_context():
    db.create_all()
    create_default_admin()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
