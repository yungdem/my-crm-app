import os
import re
import uuid
from datetime import datetime, UTC
import requests
import xml.etree.ElementTree as ET
import pandas as pd
from werkzeug.utils import secure_filename
from flask import Flask, request, render_template, redirect, url_for, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint, func
from flask_wtf.csrf import CSRFProtect
from flask_paginate import Pagination, get_page_args
from flask import Response


# --- ИНИЦИАЛИЗАЦИЯ ---
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['EXPORTS_FOLDER'] = 'exports'
app.config['SECRET_KEY'] = 'a_very_secret_and_complex_key_12345'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///crm.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
csrf = CSRFProtect(app)

os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'supplier_files'), exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'prom_exports'), exist_ok=True)
os.makedirs(os.path.join(app.config['EXPORTS_FOLDER']), exist_ok=True)

# --- МОДЕЛИ БАЗЫ ДАННЫХ ---
class Product(db.Model):
    __tablename__ = 'product'
    id = db.Column(db.Integer, primary_key=True)
    prom_product_code = db.Column(db.String(120), unique=True, nullable=False, index=True)
    product_code = db.Column(db.String(120), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    price = db.Column(db.Float, nullable=False)
    quantity = db.Column(db.Integer, default=0)
    is_available = db.Column(db.Boolean, default=False)
    size = db.Column(db.String(50), nullable=True)
    color = db.Column(db.String(50), nullable=True)

class Supplier(db.Model):
    __tablename__ = 'supplier'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    contact = db.Column(db.String(255), default='')
    contact_person = db.Column(db.String(255), default='')
    website = db.Column(db.String(255), default='')
    uploaded_products = db.relationship('SupplierUploadedProduct', back_populates='supplier', lazy=True, cascade='all, delete-orphan')

class SupplierUploadedProduct(db.Model):
    __tablename__ = 'supplier_uploaded_product'
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=False)
    product_code = db.Column(db.String(120), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=True)
    price = db.Column(db.Float, default=0.0)
    quantity = db.Column(db.Integer, default=0)
    last_uploaded = db.Column(db.DateTime, default=lambda: datetime.now(UTC))
    size = db.Column(db.String(50), nullable=True, index=True)
    supplier = db.relationship('Supplier', back_populates='uploaded_products')
    __table_args__ = (db.UniqueConstraint('supplier_id', 'product_code', 'size', name='_supplier_product_size_uc'),)

class PotentialNewPromProduct(db.Model):
    __tablename__ = 'potential_new_prom_product'
    id = db.Column(db.Integer, primary_key=True)
    product_code = db.Column(db.String(120), unique=True, nullable=False)
    supplier_name = db.Column(db.String(120))
    suggested_name = db.Column(db.String(255))
    quantity = db.Column(db.Integer)
    price = db.Column(db.Float, default=0.0)

with app.app_context():
    db.create_all()
LAST_PROMLOAD_FILENAME = None

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'xlsx', 'xls'}

def parse_data_from_file(filepath):
    products_data = []
    try:
        df = pd.read_excel(filepath)
        for _, row in df.iterrows():
            prom_product_code = str(row.get('Унікальний_ідентифікатор', '')).strip() or str(row.get('Код_товару', '')).strip()
            if not prom_product_code: continue
            product_code = str(row.get('Код_товару', '')).strip() or prom_product_code
            name = str(row.get('Назва_позиції', '')).strip()
            size = str(row.get('Размер', '')).strip()
            if not size and name:
                words = name.replace(',', ' ').split()
                for word in reversed(words):
                    if word.isdigit() and len(word) == 2:
                        size = word; break
            products_data.append({
                'prom_product_code': prom_product_code, 'product_code': product_code, 'name': name,
                'price': float(str(row.get('Ціна', 0)).replace(',', '.')), 'size': size,
                'color': str(row.get('Цвет', '')).strip()
            })
        return products_data
    except Exception as e:
        app.logger.error(f"Ошибка XLSX: {e}"); return []

def parse_supplier_yml(source):
    aggregated_offers = {}
    try:
        if source.startswith('http'):
            response = requests.get(source, timeout=120)
            response.raise_for_status()
            root = ET.fromstring(response.content)
        else:
            tree = ET.parse(source)
            root = tree.getroot()
        
        for offer in root.findall('.//offer'):
            product_code = offer.findtext('vendorCode') or offer.findtext('id') or ''
            if not product_code: continue
            
            size = next((p.text.strip() for p in offer.findall('param') if p.text and ('розмір' in p.get('name', '').lower() or 'размер' in p.get('name', '').lower() or '-' in p.get('name', ''))), '')
            
            quantity_text = offer.findtext('quantity_in_stock') or offer.findtext('quantity')
            quantity = int(quantity_text) if quantity_text and quantity_text.isdigit() else (1 if offer.get('available') == 'true' else 0)
            
            # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
            price_text = offer.findtext('price')
            price = float(price_text.strip().replace(',', '.')) if price_text and price_text.strip() else 0.0
            
            product_key = (product_code, size)
            if product_key in aggregated_offers:
                aggregated_offers[product_key]['quantity'] += quantity
            else:
                aggregated_offers[product_key] = {
                    'prom_product_code': offer.get('id') or product_code,
                    'product_code': product_code, 'size': size,
                    'name': offer.findtext('name') or '',
                    'price': price,
                    'quantity': quantity
                }
    except Exception as e:
        app.logger.error(f"Ошибка YML: {e}")
        return []
        
    return list(aggregated_offers.values())

# --- ФУНКЦИИ СИНХРОНИЗАЦИИ ---
def sync_my_products(products_from_file):
    try:
        existing_map = {p.prom_product_code: p for p in Product.query.all()}
        to_add = []
        for data in products_from_file:
            code = data.get('prom_product_code')
            if not code: continue
            
            clean_data = data.copy()
            clean_data.pop('quantity', None)
            clean_data.pop('is_available', None)
            final_data = {k: v for k, v in clean_data.items() if k in Product.__table__.columns.keys()}

            if code in existing_map:
                p = existing_map.pop(code)
                for key, value in final_data.items(): setattr(p, key, value)
            else:
                to_add.append(Product(**final_data, quantity=0, is_available=False))
        
        for p in existing_map.values(): db.session.delete(p)
        if to_add: db.session.add_all(to_add)
        
        db.session.commit()
        return {'status': 'success', 'count': len(products_from_file)}
    except Exception as e:
        db.session.rollback(); return {'status': 'error', 'message': str(e)}

def sync_supplier_products(supplier_id, products_from_file):
    try:
        existing_map = {(p.product_code, p.size): p for p in SupplierUploadedProduct.query.filter_by(supplier_id=supplier_id).all()}
        to_add = []
        for data in products_from_file:
            key = (data.get('product_code'), data.get('size'))
            clean_data = {k: v for k, v in data.items() if k in SupplierUploadedProduct.__table__.columns.keys()}
            if key in existing_map:
                p = existing_map.pop(key)
                for k, v in clean_data.items(): setattr(p, k, v)
                p.last_uploaded = datetime.now(UTC)
            else:
                to_add.append(SupplierUploadedProduct(supplier_id=supplier_id, **clean_data))
        for p in existing_map.values(): db.session.delete(p)
        if to_add: db.session.add_all(to_add)
        db.session.commit()
        return {'status': 'success', 'count': len(products_from_file)}
    except Exception as e:
        db.session.rollback(); return {'status': 'error', 'message': str(e)}

def compare_and_update_prom_quantities():
    try:
        all_supplier_products = SupplierUploadedProduct.query.all()
        supplier_stock_map = {}
        for sp in all_supplier_products:
            key = (sp.product_code, sp.size)
            supplier_stock_map[key] = supplier_stock_map.get(key, 0) + sp.quantity
        total_updated = 0
        for p in Product.query.all():
            key = (p.product_code, p.size)
            p.quantity, p.is_available = supplier_stock_map.get(key, 0), (supplier_stock_map.get(key, 0) > 0)
            total_updated += 1
        db.session.commit()
        return total_updated
    except Exception as e:
        db.session.rollback(); return 0

def get_prom_products_with_suppliers(search_query=None, page=1, per_page=30):
    query = Product.query
    if search_query: query = query.filter(Product.product_code.ilike(f"%{search_query}%"))
    pagination = query.order_by(Product.name).paginate(page=page, per_page=per_page, error_out=False)
    products_on_page = pagination.items
    supplier_prices_map = {(sp.product_code, sp.size): sp.price for sp in SupplierUploadedProduct.query.all()}
    products_data = []
    for p in products_on_page:
        supplier_price = supplier_prices_map.get((p.product_code, p.size), 0.0)
        net_profit = p.price - supplier_price if supplier_price > 0 else 0.0
        products_data.append({'product': p, 'supplier_price': supplier_price, 'net_profit': net_profit})
    return products_data, pagination
    
import pandas as pd

def generate_prom_import_file():
    global LAST_PROMLOAD_FILENAME
    if not LAST_PROMLOAD_FILENAME:
        flash('Сначала загрузите оригинальный файл выгрузки из Prom.ua.', 'error')
        return None
    
    original_prom_file = os.path.join(app.config['UPLOAD_FOLDER'], 'prom_exports', LAST_PROMLOAD_FILENAME)
    if not os.path.exists(original_prom_file):
        flash(f'Оригинальный файл Prom.ua ({LAST_PROMLOAD_FILENAME}) не найден.', 'error')
        return None

    try:
        df_original = pd.read_excel(original_prom_file)
        products_db = Product.query.all()
        quantity_map = {str(p.prom_product_code): p.quantity for p in products_db}
        # Создаем карту размеров, чтобы использовать их в ID
        size_map = {str(p.prom_product_code): p.size for p in products_db}

        def update_row(row):
            unique_id = str(row.get('Унікальний_ідентифікатор', '')).strip() or str(row.get('Код_товару', '')).strip()
            
            # Обновляем количество и наличие
            if unique_id in quantity_map:
                new_quantity = quantity_map.get(unique_id, 0)
                row['Кількість'] = new_quantity
                row['Наявність'] = '+' if new_quantity > 0 else '-'
            
            # --- НАЧАЛО НОВОЙ ЛОГИКИ ГЕНЕРАЦИИ ID ---
            # Генерируем случайную часть ID
            random_part = uuid.uuid4().hex[:10]
            
            # Получаем размер для этого товара
            size = size_map.get(unique_id)
            
            # Собираем ID в нужном формате
            if size:
                row['Ідентифікатор_товару'] = f"{random_part}s{size}"
            else:
                row['Ідентифікатор_товару'] = random_part
            # --- КОНЕЦ НОВОЙ ЛОГИКИ ---
            
            # Добавляем обязательное поле "Одиниця_виміру"
            if 'Одиниця_виміру' not in row or pd.isna(row.get('Одиниця_виміру')):
                row['Одиниця_виміру'] = 'шт.'
            
            return row

        df_updated = df_original.apply(update_row, axis=1)
        
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        export_filename = f'prom_update_{timestamp}.xlsx'
        export_path = os.path.join(app.config['EXPORTS_FOLDER'], export_filename)
        
        # Убедимся, что новая колонка есть в итоговом файле
        if 'Ідентифікатор_товару' not in df_updated.columns:
            # Эта строка на случай, если apply не добавит колонку для пустого DataFrame
            df_updated['Ідентифікатор_товару'] = ''

        df_updated.to_excel(export_path, index=False)
        
        flash(f'Файл для импорта "{export_filename}" успешно создан!', 'success')
        return export_path
    except Exception as e:
        flash(f'Ошибка при генерации файла для Prom.ua: {e}', 'error')
        app.logger.error(f"Ошибка при генерации файла Prom.ua: {e}")
        return None
        
def get_potential_new_prom_products():
    return PotentialNewPromProduct.query.order_by(PotentialNewPromProduct.product_code).all()

# --- МАРШРУТЫ FLASK ---
@app.route('/')
def dashboard():
    stats = {'total_products': Product.query.count(), 'products_in_stock': Product.query.filter(Product.quantity > 0).count(), 'supplier_count': Supplier.query.count(), 'new_products_count': PotentialNewPromProduct.query.count()}
    return render_template('dashboard.html', stats=stats)

@app.route('/products', methods=['GET', 'POST'])
def view_my_products():
    if request.method == 'POST':
        action_type = request.form.get('action_type')
        if action_type == 'import_prom_file':
            file = request.files.get('file')
            if not file or not file.filename: flash('Файл не был выбран.', 'warning')
            else:
                try:
                    filename = secure_filename(file.filename)
                    filepath = os.path.join(app.config['UPLOAD_FOLDER'], 'prom_exports', filename)
                    file.save(filepath)
                    global LAST_PROMLOAD_FILENAME; LAST_PROMLOAD_FILENAME = filename
                    data = parse_data_from_file(filepath)
                    if data:
                        result = sync_my_products(data)
                        if result['status'] == 'success': flash(f'Синхронизация из файла завершена.', 'success')
                        else: flash(f"Ошибка: {result['message']}", 'error')
                    else: flash('Файл пуст или не удалось обработать.', 'warning')
                except Exception as e: flash(f'Критическая ошибка: {e}', 'error')
        
        elif action_type == 'import_prom_yml_url':
            yml_url = request.form.get('yml_url')
            if not yml_url: flash('URL не указан.', 'danger')
            else:
                try:
                    data = parse_supplier_yml(yml_url)
                    if data:
                        result = sync_my_products(data)
                        if result['status'] == 'success': flash(f'Синхронизация по YML завершена.', 'success')
                        else: flash(f"Ошибка: {result['message']}", 'error')
                    else: flash('Не удалось найти товары в YML.', 'warning')
                except Exception as e: flash(f'Ошибка при импорте YML: {e}', 'danger')

        elif action_type == 'compare_and_update':
            count = compare_and_update_prom_quantities()
            flash(f'Наличие для {count} товаров было обновлено.', 'success')
        
        return redirect(url_for('view_my_products'))
    
    page, per_page, _ = get_page_args(page_parameter='page', per_page_parameter='per_page', per_page_default=30)
    search_query = request.args.get('search')
    products_data, sa_pagination = get_prom_products_with_suppliers(search_query=search_query, page=page, per_page=per_page)
    pagination = Pagination(page=page, per_page=per_page, total=sa_pagination.total, css_framework='bootstrap5', record_name='товаров')
    return render_template('my_products.html', products_data=products_data, pagination=pagination, search_query=search_query)

@app.route('/suppliers')
def view_suppliers():
    return render_template('suppliers_list.html', suppliers=Supplier.query.order_by(Supplier.name).all())

@app.route('/supplier/<int:supplier_id>')
def supplier_details(supplier_id):
    supplier = db.session.get(Supplier, supplier_id)
    if not supplier: return redirect(url_for('view_suppliers'))
    products = SupplierUploadedProduct.query.filter_by(supplier_id=supplier_id).order_by(SupplierUploadedProduct.product_code, SupplierUploadedProduct.size).all()
    return render_template('supplier_details.html', supplier=supplier, products=products)

@app.route('/add_supplier', methods=['GET', 'POST'])
def add_supplier_route():
    form_data = request.form
    if request.method == 'POST':
        supplier_id = form_data.get('supplier_id')
        supplier = db.session.get(Supplier, int(supplier_id)) if supplier_id else Supplier()
        if not supplier_id: db.session.add(supplier)
        supplier.name = form_data['name']
        supplier.contact = form_data.get('contact', '')
        supplier.contact_person = form_data.get('contact_person', '')
        supplier.website = form_data.get('website', '')
        try:
            db.session.commit()
            flash('Поставщик успешно сохранен!', 'success')
        except Exception:
            db.session.rollback(); flash(f'Ошибка: Имя "{form_data["name"]}" уже существует.', 'danger')
        return redirect(url_for('view_suppliers'))
    supplier = db.session.get(Supplier, int(request.args.get('id'))) if request.args.get('id') else None
    return render_template('add_supplier_form.html', supplier=supplier)

@app.route('/edit_supplier/<int:supplier_id>')
def edit_supplier_route(supplier_id):
    return render_template('add_supplier_form.html', supplier=db.session.get(Supplier, supplier_id))

@app.route('/supplier/<int:supplier_id>/delete', methods=['POST'])
def delete_supplier_route(supplier_id):
    supplier = db.session.get(Supplier, supplier_id)
    if supplier:
        db.session.delete(supplier); db.session.commit()
        flash(f"Поставщик '{supplier.name}' удален.", 'success')
    return redirect(url_for('view_suppliers'))

@app.route('/import/supplier/file/<int:supplier_id>', methods=['POST'])
def import_supplier_file(supplier_id):
    if not db.session.get(Supplier, supplier_id): return redirect(url_for('view_suppliers'))
    file = request.files.get('file')
    if not file or not file.filename:
        flash("Файл не выбран.", "error"); return redirect(url_for('supplier_details', supplier_id=supplier_id))
    try:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], 'supplier_files', filename)
        file.save(filepath)
        data = parse_supplier_yml(filepath)
        os.remove(filepath)
        result = sync_supplier_products(supplier_id, data)
        if result['status'] == 'success': flash(f'Синхронизация завершена.', 'success')
        else: flash(f"Ошибка: {result['message']}", 'error')
    except Exception as e: flash(f'Критическая ошибка: {e}', 'error')
    return redirect(url_for('supplier_details', supplier_id=supplier_id))

@app.route('/import/supplier/yml', methods=['POST'])
def import_supplier_from_yml():
    yml_url, supplier_id = request.form.get('yml_url'), int(request.form.get('supplier_id'))
    if not yml_url: flash('URL не указан.', 'danger')
    else:
        try:
            data = parse_supplier_yml(yml_url)
            if data:
                result = sync_supplier_products(supplier_id, data)
                if result['status'] == 'success': flash(f'Синхронизация по ссылке завершена.', 'success')
                else: flash(f"Ошибка: {result['message']}", 'error')
            else: flash('Не удалось найти товары в YML.', 'warning')
        except Exception as e: flash(f'Ошибка при импорте YML: {e}', 'danger')
    return redirect(url_for('supplier_details', supplier_id=supplier_id))
    
@app.route('/new_products')
def view_new_products():
    return render_template('new_products.html', products=get_potential_new_prom_products())

@app.route('/approve_new_prom_product/<string:product_code>', methods=['POST'])
def approve_new_prom_product(product_code):
    flash("Функционал в разработке.", "info"); return redirect(url_for('view_new_products'))

@app.route('/reject_new_prom_product/<string:product_code>', methods=['POST'])
def reject_new_prom_product(product_code):
    flash("Функционал в разработке.", "info"); return redirect(url_for('view_new_products'))
    
@app.route('/export/prom')
def export_prom_file():
    export_path = generate_prom_import_file()
    return send_file(export_path, as_attachment=True) if export_path else redirect(url_for('view_my_products'))
    
def generate_yml_export_content():
    """
    Генерирует содержимое YML-файла на основе данных из базы.
    """
    # Создаем базовую структуру YML
    yml_catalog = ET.Element('yml_catalog', date=datetime.now(UTC).strftime('%Y-%m-%d %H:%M'))
    shop = ET.SubElement(yml_catalog, 'shop')
    
    ET.SubElement(shop, 'name').text = "My CRM Export"
    ET.SubElement(shop, 'company').text = "My Company"
    ET.SubElement(shop, 'url').text = request.host_url
    
    currencies = ET.SubElement(shop, 'currencies')
    ET.SubElement(currencies, 'currency', id='UAH', rate='1')
    
    categories = ET.SubElement(shop, 'categories')
    # Здесь можно добавить логику для экспорта категорий, если нужно
    ET.SubElement(categories, 'category', id='1').text = "Общая категория"
    
    offers = ET.SubElement(shop, 'offers')
    
    # Получаем все наши товары с актуальным наличием
    products_to_export = Product.query.all()
    
    for product in products_to_export:
        offer = ET.SubElement(offers, 'offer', id=str(product.prom_product_code), available=str(product.is_available).lower())
        
        ET.SubElement(offer, 'name').text = product.name
        ET.SubElement(offer, 'vendorCode').text = product.product_code
        ET.SubElement(offer, 'price').text = str(product.price)
        ET.SubElement(offer, 'currencyId').text = 'UAH'
        ET.SubElement(offer, 'categoryId').text = '1' # Используем ID общей категории
        ET.SubElement(offer, 'quantity_in_stock').text = str(product.quantity)
        
        if product.size:
            param = ET.SubElement(offer, 'param', name='Размер')
            param.text = product.size
        
        if product.color:
            param = ET.SubElement(offer, 'param', name='Цвет')
            param.text = product.color

    # Преобразуем XML-дерево в строку с правильным форматированием
    ET.indent(yml_catalog)
    xml_string = ET.tostring(yml_catalog, encoding='unicode', xml_declaration=True)
    return xml_string


@app.route('/export.yml')
def export_yml_feed():
    """
    Этот маршрут отдает сгенерированный YML файл.
    """
    yml_content = generate_yml_export_content()
    return Response(yml_content, mimetype='application/xml')
    
    
# --- ЗАПУСК ПРИЛОЖЕНИЯ ---
if __name__ == '__main__':
    print("="*50 + "\nЗапуск CRM-системы. Откройте: http://127.0.0.1:5000\n" + "="*50)
    app.run(debug=True, port=5000)
