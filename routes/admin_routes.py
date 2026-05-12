"""
Admin Routes for Ethiosadat Furniture

This module contains all admin panel routes including:
- Dashboard and statistics
- Product management (CRUD)
- Advertisement management
- Order management
- Push notifications
- Reports and analytics
"""

from flask import render_template, request, redirect, url_for, flash, jsonify, session
from middleware.auth import login_required
from database.models import Product, Ad, Order
from database.db import get_db
import os
import json
from datetime import datetime

# ==================== ADMIN BLUEPRINT ====================

from flask import Blueprint
admin_bp = Blueprint('admin', __name__)


# ==================== ADMIN LOGIN ====================

@admin_bp.route('/login', methods=['GET', 'POST'])
def admin_login():
    """Admin login page"""
    # If already logged in, redirect to dashboard
    if session.get('admin'):
        return redirect(url_for('admin.dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        
        # Get admin credentials from environment
        import os
        admin_username = os.environ.get('ADMIN_USERNAME', 'admin')
        admin_password = os.environ.get('ADMIN_PASSWORD', 'admin123456')
        
        if username == admin_username and password == admin_password:
            session['admin'] = True
            session['admin_username'] = username
            flash('Logged in successfully!', 'success')
            
            # Redirect to next page if exists
            next_page = request.args.get('next')
            if next_page:
                return redirect(next_page)
            return redirect(url_for('admin.dashboard'))
        else:
            flash('Invalid username or password!', 'danger')
    
    return render_template('admin/login.html')


@admin_bp.route('/logout')
def admin_logout():
    """Admin logout"""
    session.pop('admin', None)
    session.pop('admin_username', None)
    flash('Logged out successfully!', 'success')
    return redirect(url_for('admin.admin_login'))


# ==================== DASHBOARD ====================

@admin_bp.route('/dashboard')
@login_required
def dashboard():
    """Admin dashboard with statistics"""
    db = get_db()
    cursor = db.cursor()
    
    # Get statistics
    cursor.execute("SELECT COUNT(*) FROM products WHERE is_active = 1")
    products_count = cursor.fetchone()[0] or 0
    
    cursor.execute("SELECT COUNT(*) FROM advertisements WHERE is_active = 1")
    ads_count = cursor.fetchone()[0] or 0
    
    cursor.execute("SELECT COUNT(*) FROM orders")
    total_orders = cursor.fetchone()[0] or 0
    
    cursor.execute("SELECT COUNT(*) FROM orders WHERE status = 'pending'")
    pending_orders = cursor.fetchone()[0] or 0
    
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_admin = 0")
    customers_count = cursor.fetchone()[0] or 0
    
    cursor.execute("SELECT SUM(total) FROM orders WHERE status != 'cancelled'")
    total_revenue = cursor.fetchone()[0] or 0
    
    cursor.execute("SELECT COUNT(*) FROM orders WHERE DATE(created_at) = CURRENT_DATE")
    today_orders = cursor.fetchone()[0] or 0
    
    # Get recent orders
    cursor.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 10")
    recent_orders = cursor.fetchall()
    
    # Get low stock products
    cursor.execute("SELECT * FROM products WHERE stock_quantity <= low_stock_threshold AND stock_quantity > 0 LIMIT 10")
    low_stock_products = cursor.fetchall()
    
    stats = {
        'products_count': products_count,
        'ads_count': ads_count,
        'total_orders': total_orders,
        'pending_orders': pending_orders,
        'customers_count': customers_count,
        'total_revenue': total_revenue,
        'today_orders': today_orders
    }
    
    return render_template('admin/dashboard.html', 
                         stats=stats, 
                         recent_orders=recent_orders,
                         low_stock_products=low_stock_products)


# ==================== PRODUCT MANAGEMENT ====================

@admin_bp.route('/products')
@login_required
def products():
    """List all products"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT p.*, c.name as category_name 
        FROM products p
        LEFT JOIN categories c ON p.category_id = c.id
        ORDER BY p.id DESC
    """)
    products = cursor.fetchall()
    return render_template('admin/products/index.html', products=products)


@admin_bp.route('/products/create', methods=['GET', 'POST'])
@login_required
def product_create():
    """Create new product"""
    db = get_db()
    cursor = db.cursor()
    
    # Get categories for dropdown
    cursor.execute("SELECT id, name, name_am FROM categories ORDER BY sort_order")
    categories = cursor.fetchall()
    
    if request.method == 'POST':
        # Get form data
        name = request.form.get('name', '')
        name_am = request.form.get('name_am', '')
        name_ar = request.form.get('name_ar', '')
        description = request.form.get('description', '')
        description_am = request.form.get('description_am', '')
        description_ar = request.form.get('description_ar', '')
        price = float(request.form.get('price', 0))
        compare_price = request.form.get('compare_price')
        compare_price = float(compare_price) if compare_price else None
        stock_quantity = int(request.form.get('stock_quantity', 0))
        category_id = int(request.form.get('category_id', 0))
        material = request.form.get('material', '')
        color = request.form.get('color', '')
        sku = request.form.get('sku', '')
        is_featured = 1 if request.form.get('is_featured') else 0
        is_new = 1 if request.form.get('is_new') else 0
        
        # Handle image upload
        image = request.files.get('image')
        image_filename = ''
        if image and image.filename:
            from werkzeug.utils import secure_filename
            import uuid
            filename = secure_filename(image.filename)
            ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
            unique_filename = f"product_{uuid.uuid4().hex[:8]}.{ext}"
            
            upload_dir = 'static/uploads/products'
            os.makedirs(upload_dir, exist_ok=True)
            image.save(os.path.join(upload_dir, unique_filename))
            image_filename = f'/uploads/products/{unique_filename}'
        
        # Insert product
        cursor.execute("""
            INSERT INTO products (
                name, name_am, name_ar, description, description_am, description_ar,
                price, compare_price, stock_quantity, category_id, material, color,
                sku, is_featured, is_new, thumbnail, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (name, name_am, name_ar, description, description_am, description_ar,
              price, compare_price, stock_quantity, category_id, material, color,
              sku, is_featured, is_new, image_filename))
        
        db.commit()
        flash('Product created successfully!', 'success')
        return redirect(url_for('admin.products'))
    
    return render_template('admin/products/create.html', categories=categories)


@admin_bp.route('/products/edit/<int:pid>', methods=['GET', 'POST'])
@login_required
def product_edit(pid):
    """Edit existing product"""
    db = get_db()
    cursor = db.cursor()
    
    # Get product
    cursor.execute("SELECT * FROM products WHERE id = ?", (pid,))
    product = cursor.fetchone()
    
    if not product:
        flash('Product not found!', 'danger')
        return redirect(url_for('admin.products'))
    
    # Get categories
    cursor.execute("SELECT id, name, name_am FROM categories ORDER BY sort_order")
    categories = cursor.fetchall()
    
    if request.method == 'POST':
        # Get form data
        name = request.form.get('name', '')
        name_am = request.form.get('name_am', '')
        name_ar = request.form.get('name_ar', '')
        description = request.form.get('description', '')
        description_am = request.form.get('description_am', '')
        description_ar = request.form.get('description_ar', '')
        price = float(request.form.get('price', 0))
        compare_price = request.form.get('compare_price')
        compare_price = float(compare_price) if compare_price else None
        stock_quantity = int(request.form.get('stock_quantity', 0))
        category_id = int(request.form.get('category_id', 0))
        material = request.form.get('material', '')
        color = request.form.get('color', '')
        sku = request.form.get('sku', '')
        is_featured = 1 if request.form.get('is_featured') else 0
        is_new = 1 if request.form.get('is_new') else 0
        
        # Handle image upload
        image = request.files.get('image')
        image_filename = product['thumbnail']
        if image and image.filename:
            from werkzeug.utils import secure_filename
            import uuid
            filename = secure_filename(image.filename)
            ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
            unique_filename = f"product_{uuid.uuid4().hex[:8]}.{ext}"
            
            upload_dir = 'static/uploads/products'
            os.makedirs(upload_dir, exist_ok=True)
            image.save(os.path.join(upload_dir, unique_filename))
            image_filename = f'/uploads/products/{unique_filename}'
        
        # Update product
        cursor.execute("""
            UPDATE products SET
                name=?, name_am=?, name_ar=?, description=?, description_am=?, description_ar=?,
                price=?, compare_price=?, stock_quantity=?, category_id=?, material=?, color=?,
                sku=?, is_featured=?, is_new=?, thumbnail=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (name, name_am, name_ar, description, description_am, description_ar,
              price, compare_price, stock_quantity, category_id, material, color,
              sku, is_featured, is_new, image_filename, pid))
        
        db.commit()
        flash('Product updated successfully!', 'success')
        return redirect(url_for('admin.products'))
    
    return render_template('admin/products/edit.html', product=product, categories=categories)


@admin_bp.route('/products/delete/<int:pid>')
@login_required
def product_delete(pid):
    """Delete product (soft delete)"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("UPDATE products SET is_active = 0 WHERE id = ?", (pid,))
    db.commit()
    flash('Product deleted successfully!', 'success')
    return redirect(url_for('admin.products'))


# ==================== ADVERTISEMENT MANAGEMENT ====================

@admin_bp.route('/ads')
@login_required
def ads():
    """List all advertisements"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM advertisements ORDER BY sort_order ASC, id DESC")
    ads_list = cursor.fetchall()
    return render_template('admin/ads/index.html', ads=ads_list)


@admin_bp.route('/ads/create', methods=['GET', 'POST'])
@login_required
def ad_create():
    """Create new advertisement"""
    if request.method == 'POST':
        title = request.form.get('title', '')
        title_am = request.form.get('title_am', '')
        title_ar = request.form.get('title_ar', '')
        description = request.form.get('description', '')
        description_am = request.form.get('description_am', '')
        description_ar = request.form.get('description_ar', '')
        link = request.form.get('link', '')
        sort_order = int(request.form.get('sort_order', 0))
        
        # Handle image upload
        image = request.files.get('image')
        image_filename = ''
        if image and image.filename:
            from werkzeug.utils import secure_filename
            import uuid
            filename = secure_filename(image.filename)
            ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
            unique_filename = f"ad_{uuid.uuid4().hex[:8]}.{ext}"
            
            upload_dir = 'static/uploads/ads'
            os.makedirs(upload_dir, exist_ok=True)
            image.save(os.path.join(upload_dir, unique_filename))
            image_filename = f'/uploads/ads/{unique_filename}'
        
        db = get_db()
        cursor = db.cursor()
        cursor.execute("""
            INSERT INTO advertisements (
                title, title_am, title_ar, description, description_am, description_ar,
                image, link, sort_order, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (title, title_am, title_ar, description, description_am, description_ar,
              image_filename, link, sort_order))
        
        db.commit()
        flash('Advertisement created successfully!', 'success')
        return redirect(url_for('admin.ads'))
    
    return render_template('admin/ads/create.html')


@admin_bp.route('/ads/edit/<int:aid>', methods=['GET', 'POST'])
@login_required
def ad_edit(aid):
    """Edit advertisement"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM advertisements WHERE id = ?", (aid,))
    ad = cursor.fetchone()
    
    if not ad:
        flash('Advertisement not found!', 'danger')
        return redirect(url_for('admin.ads'))
    
    if request.method == 'POST':
        title = request.form.get('title', '')
        title_am = request.form.get('title_am', '')
        title_ar = request.form.get('title_ar', '')
        description = request.form.get('description', '')
        description_am = request.form.get('description_am', '')
        description_ar = request.form.get('description_ar', '')
        link = request.form.get('link', '')
        sort_order = int(request.form.get('sort_order', 0))
        
        # Handle image upload
        image = request.files.get('image')
        image_filename = ad['image']
        if image and image.filename:
            from werkzeug.utils import secure_filename
            import uuid
            filename = secure_filename(image.filename)
            ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
            unique_filename = f"ad_{uuid.uuid4().hex[:8]}.{ext}"
            
            upload_dir = 'static/uploads/ads'
            os.makedirs(upload_dir, exist_ok=True)
            image.save(os.path.join(upload_dir, unique_filename))
            image_filename = f'/uploads/ads/{unique_filename}'
        
        cursor.execute("""
            UPDATE advertisements SET
                title=?, title_am=?, title_ar=?, description=?, description_am=?, description_ar=?,
                image=?, link=?, sort_order=?
            WHERE id=?
        """, (title, title_am, title_ar, description, description_am, description_ar,
              image_filename, link, sort_order, aid))
        
        db.commit()
        flash('Advertisement updated successfully!', 'success')
        return redirect(url_for('admin.ads'))
    
    return render_template('admin/ads/edit.html', ad=ad)


@admin_bp.route('/ads/toggle/<int:aid>')
@login_required
def ad_toggle(aid):
    """Toggle advertisement active status"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("UPDATE advertisements SET is_active = NOT is_active WHERE id = ?", (aid,))
    db.commit()
    flash('Advertisement status toggled!', 'success')
    return redirect(url_for('admin.ads'))


@admin_bp.route('/ads/delete/<int:aid>')
@login_required
def ad_delete(aid):
    """Delete advertisement"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("DELETE FROM advertisements WHERE id = ?", (aid,))
    db.commit()
    flash('Advertisement deleted successfully!', 'success')
    return redirect(url_for('admin.ads'))


# ==================== ORDER MANAGEMENT ====================

@admin_bp.route('/orders')
@login_required
def orders():
    """List all orders"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM orders ORDER BY id DESC")
    orders_list = cursor.fetchall()
    return render_template('admin/orders/index.html', orders=orders_list)


@admin_bp.route('/orders/view/<int:oid>')
@login_required
def order_view(oid):
    """View order details"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM orders WHERE id = ?", (oid,))
    order = cursor.fetchone()
    
    if not order:
        flash('Order not found!', 'danger')
        return redirect(url_for('admin.orders'))
    
    # Get order items
    cursor.execute("""
        SELECT oi.*, p.name, p.name_am, p.name_ar, p.thumbnail
        FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        WHERE oi.order_id = ?
    """, (oid,))
    items = cursor.fetchall()
    
    return render_template('admin/orders/view.html', order=order, items=items)


@admin_bp.route('/orders/update-status/<int:oid>', methods=['POST'])
@login_required
def order_update_status(oid):
    """Update order status"""
    status = request.form.get('status')
    db = get_db()
    cursor = db.cursor()
    cursor.execute("UPDATE orders SET status = ? WHERE id = ?", (status, oid))
    db.commit()
    flash('Order status updated!', 'success')
    return redirect(url_for('admin.order_view', oid=oid))


# ==================== PUSH NOTIFICATIONS ====================

@admin_bp.route('/send-notification', methods=['GET', 'POST'])
@login_required
def send_notification():
    """Send push notifications to users"""
    if request.method == 'POST':
        title = request.form.get('title', '')
        body = request.form.get('body', '')
        title_am = request.form.get('title_am', '')
        body_am = request.form.get('body_am', '')
        title_ar = request.form.get('title_ar', '')
        body_ar = request.form.get('body_ar', '')
        link = request.form.get('link', '')
        target = request.form.get('target', 'all')
        
        # Save notification to database
        db = get_db()
        cursor = db.cursor()
        cursor.execute("""
            INSERT INTO notifications (title, title_am, title_ar, body, body_am, body_ar, link, target_audience, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (title, title_am, title_ar, body, body_am, body_ar, link, target))
        db.commit()
        
        flash('Notification saved! (FCM integration will be added later)', 'success')
        return redirect(url_for('admin.send_notification'))
    
    return render_template('admin/send_notification.html')


# ==================== SETTINGS ====================

@admin_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    """Admin settings page"""
    db = get_db()
    cursor = db.cursor()
    
    if request.method == 'POST':
        # Update settings
        settings = {
            'site_name': request.form.get('site_name', ''),
            'site_email': request.form.get('site_email', ''),
            'site_phone': request.form.get('site_phone', ''),
            'whatsapp_number': request.form.get('whatsapp_number', ''),
            'free_shipping_threshold': request.form.get('free_shipping_threshold', '5000'),
            'shipping_cost': request.form.get('shipping_cost', '200'),
            'default_language': request.form.get('default_language', 'am')
        }
        
        for key, value in settings.items():
            cursor.execute("""
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = CURRENT_TIMESTAMP
            """, (key, value, value))
        
        db.commit()
        flash('Settings saved successfully!', 'success')
        return redirect(url_for('admin.settings'))
    
    # Get current settings
    cursor.execute("SELECT key, value FROM settings")
    settings_rows = cursor.fetchall()
    settings = {row['key']: row['value'] for row in settings_rows}
    
    return render_template('admin/settings.html', settings=settings)


# ==================== REPORTS ====================

@admin_bp.route('/reports')
@login_required
def reports():
    """Reports dashboard"""
    db = get_db()
    cursor = db.cursor()
    
    # Get sales by day (last 7 days)
    cursor.execute("""
        SELECT DATE(created_at) as day, COUNT(*) as count, SUM(total) as revenue
        FROM orders
        WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
        GROUP BY DATE(created_at)
        ORDER BY day DESC
    """)
    sales_by_day = cursor.fetchall()
    
    # Get top selling products
    cursor.execute("""
        SELECT p.id, p.name, p.name_am, SUM(oi.quantity) as total_sold
        FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        GROUP BY p.id
        ORDER BY total_sold DESC
        LIMIT 10
    """)
    top_products = cursor.fetchall()
    
    return render_template('admin/reports/index.html', 
                         sales_by_day=sales_by_day,
                         top_products=top_products)