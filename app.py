# ==================== 1. IMPORT STATEMENTS ====================

# Standard Library Imports
import os
import sys
import json
import uuid
import time
import sqlite3
import logging
import atexit
import urllib.parse
from datetime import datetime, timedelta
from functools import wraps
from decimal import Decimal
import flask_caching
import re
import datetime as datetime_
# Third Party Imports
from flask import (
    Flask, render_template, request, redirect, url_for,
    abort, session, flash, jsonify, send_from_directory, make_response, g
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Local Imports
from config import Config
from database.db import get_db, init_db, commit_or_rollback
from database.models import (
    User, Category, Product, CartItem, Order, OrderItem, Advertisement, Branch, Notification
)
from middleware.auth import login_required, admin_required, user_login_required, get_current_user, is_authenticated
from middleware.platform import get_platform, is_android_app
from utils.translation_cache import translate_text, batch_translate, clear_translation_cache, get_translation_stats, FALLBACK_TEXTS


# ==================== 2. APP INITIALIZATION & CONFIGURATION ====================

app = Flask(__name__)

# ---- Live Visitor Tracking (in-memory, 5-minute window) ----
import threading
_visitor_lock = threading.Lock()
_active_visitors = {}   # {session_id: last_seen_timestamp}


class SafeJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if hasattr(obj, '__class__') and 'Undefined' in str(obj.__class__):
            return None
        try:
            return super().default(obj)
        except TypeError:
            return str(obj)


app.json_encoder = SafeJSONEncoder

# Register template filters
def format_price_number(value):
    try:
        if isinstance(value, (int, float)):
            return f"{value:,.0f}"
        num = float(value)
        return f"{num:,.0f}"
    except (ValueError, TypeError):
        return str(value)


def format_price(value):
    try:
        num = float(value)
        return f"{num:,.0f} ETB"
    except (ValueError, TypeError):
        return str(value)


app.jinja_env.filters["format_price_number"] = format_price_number
app.jinja_env.filters["format_price"] = format_price
app.jinja_env.globals["format_price"] = format_price
app.jinja_env.globals["format_price_number"] = format_price_number

app.config.from_object(Config)
_secret_key = os.environ.get('SECRET_KEY')
if not _secret_key:
    import secrets as _secrets
    _secret_key = _secrets.token_hex(32)
    print("WARNING: SECRET_KEY not set in environment. Sessions will not persist across restarts. Set SECRET_KEY in environment variables.")
app.secret_key = _secret_key

# Rate Limiter
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["2000000 per day", "100000 per hour"],
    storage_uri="memory://"
)

# Upload configuration
UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'mp4', 'webm', 'mov'}
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB

# Create upload directories
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(os.path.join(UPLOAD_FOLDER, 'products'), exist_ok=True)
os.makedirs(os.path.join(UPLOAD_FOLDER, 'ads'), exist_ok=True)
os.makedirs('logs', exist_ok=True)


def allowed_file(filename):
    """Check if file extension is allowed"""
    if not filename or '.' not in filename:
        return False
    return filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

file_handler = logging.FileHandler('logs/app.log')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

security_handler = logging.FileHandler('logs/security.log')
security_handler.setLevel(logging.WARNING)
security_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

app.logger.addHandler(file_handler)
app.logger.addHandler(security_handler)

app.logger.info("=" * 50)
app.logger.info("Ethiosadat Application Started")
app.logger.info("=" * 50)

# Environment variables
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123456')
WHATSAPP_NUMBER = os.environ.get('WHATSAPP_NUMBER', '251906020606')


# ==================== 3. DATABASE INITIALIZATION ====================

def init_db_tables():
    """Initialize database with all tables"""
    from database.db import init_db
    init_db()


def seed_categories_if_empty():
    """Seed default categories on startup if the table is empty (PostgreSQL)."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM categories")
        count = cur.fetchone()[0]
        if count == 0:
            defaults = [
                ('Living Room', 'ሳሎን', 'غرفة المعيشة', '🛋️', 1),
                ('Bedroom', 'መኝታ ክፍል', 'غرفة النوم', '🛏️', 2),
                ('Office', 'ቢሮ', 'مكتب', '💼', 3),
                ('Dining', 'መመገቢያ', 'غرفة الطعام', '🍽️', 4),
            ]
            cur.executemany(
                "INSERT INTO categories (name, name_am, name_ar, icon, sort_order) VALUES (?, ?, ?, ?, ?)",
                defaults
            )
            conn.commit()
            print(f"✅ Seeded {len(defaults)} default categories")
    except Exception as _e:
        print(f"seed_categories_if_empty error: {_e}")


# Initialize database on startup
with app.app_context():
    init_db_tables()
    seed_categories_if_empty()


# ==================== 4. CONTEXT PROCESSORS ====================

@app.context_processor
def inject_globals():
    """Inject global variables into all templates."""
    lang = session.get('lang', 'am')
    current_user = get_current_user()
    platform = get_platform()

    pending_orders_count = 0
    low_stock_count = 0
    if session.get('admin') or session.get('is_admin'):
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM orders WHERE status = 'pending'")
            row = cur.fetchone()
            pending_orders_count = row[0] if row else 0

            cur.execute("""
                SELECT COUNT(*) FROM products
                WHERE stock_quantity <= low_stock_threshold AND stock_quantity > 0
            """)
            ls_row = cur.fetchone()
            low_stock_count = ls_row[0] if ls_row else 0

        except Exception:
            pass

    return {
        'current_year': datetime_.datetime.now().year,
        'lang': lang,
        'current_user': current_user,
        'is_authenticated': is_authenticated(),
        'is_admin': session.get('admin', False),
        'is_android_app': is_android_app(),
        'platform': platform,
        'whatsapp_number': WHATSAPP_NUMBER,
        'google_translate_widget': get_google_translate_widget(),
        'app_name': 'Ethiosadat Furniture',
        'pending_orders_count': pending_orders_count,
        'low_stock_count': low_stock_count,
        'is_discount_user': session.get('user_id') is not None,
        'discount_message': 'የ 10% ቅናሽ ተጠቃሚ ነዎት! / You are a 10% discount user!' if session.get('user_id') else '',
    }


def get_google_translate_widget():
    """Generate Google Translate widget for 3 languages"""
    return '''
    <div id="google_translate_element" style="position: fixed; bottom: 20px; right: 20px; z-index: 1000;"></div>
    <script type="text/javascript">
        function googleTranslateElementInit() {
            new google.translate.TranslateElement({
                pageLanguage: 'am',
                includedLanguages: 'am,en,ar',
                layout: google.translate.TranslateElement.InlineLayout.SIMPLE,
                autoDisplay: false
            }, 'google_translate_element');
        }
    </script>
    <script type="text/javascript" src="//translate.google.com/translate_a/element.js?cb=googleTranslateElementInit"></script>
    '''


# ==================== 5. LANGUAGE / LOCALIZATION ====================

TEXTS = {
    'am': {
        # Search page
        'search_results': '🔍 የፍለጋ ውጤቶች',
        'find_perfect': 'ለቤትዎ ምርጥ የቤት እቃ ያግኙ',
        'showing_results_for': '🔎 ውጤቶች ለ',
        'products_found': 'ምርቶች ተገኝተዋል',
        'no_products_found': 'ምርት አልተገኘም',
        'we_couldnt_find': 'ለሚፈልጉት ቃል ምርት አልተገኘም',
        'try_tips': '💡 ይሞክሩ:',
        'tip_different_keywords': '• ሌሎች ቃላት ይጠቀሙ',
        'tip_check_spelling': '• ፊደሉን ያረጋግጡ',
        'tip_browse_categories': '• ምድቦቹን ያስሱ',
        'back_to_home': '← ወደ መነሻ ተመለስ',
        'popular_searches': '💡 ተወዳጅ ፍለጋዎች:',
        'try_searching_for': '💡 ለፍለጋ ይሞክሩ:',
        'sort_relevance': '📌 ተዛማጅነት',
        'sort_price_low_high': '💰 ዋጋ: ዝቅ → ከፍ',
        'sort_price_high_low': '💰 ዋጋ: ከፍ → ዝቅ',
        'sort_name_az': '📝 ስም: ሀ-ፐ',
        'sort_name_za': '📝 ስም: ፐ-ሀ',
        'filter_products': '🔍 ምርቶችን አጣራ',
        # Navigation & UI
        'search': 'እቃዎችን እዚህ ይፈልጉ...',
        'home': 'መነሻ',
        'products': 'ምርቶች',
        'contact': 'አግኙን',
        'about_us': 'ስለ እኛ',
        'our_branches': 'ቅርንጫፎቻችን',
        'all': 'ሁሉም',
        'all_products': 'ሁሉም ምርቶች',
        'categories': 'ምድቦች',
        'featured_products': '⭐ ተመራጭ ምርቶች',
        'new_arrivals': '🆕 አዲስ ምርቶች',
        'add_to_cart': '🛒 ወደ ጋሪ ጨምር',
        'view_details': 'ዝርዝር ይመልከቱ →',
        'shop_now': 'አሁን ይግዙ →',
        'login': 'ግባ',
        'register': 'ተመዝገብ',
        'logout': 'ውጣ',
        'profile': 'መገለጫ',
        'my_orders': 'የእኔ ትዕዛዞች',
        'my_cart': 'የእኔ ጋሪ',
        'in_stock': 'አለ',
        'out_of_stock': 'የለም',
        'quick_links': 'ፈጣን አገናኞች',
        'call_us': 'ይደውሉልን',
        'quality_tagline': 'ጥራት ያለው የቤት እቃ በተመጣጣኝ ዋጋ',
        'free_shipping_msg': '🚚 ከ5,000 ብር በላይ ትዕዛዝ ነጻ ማጓጓዝ',
        'copyright_text': 'መብቱ በህግ የተጠበቀ ነው',
        # Product form
        'order_now': 'አሁን እዘዝ',
        'address': 'አድራሻ፦ አዲስ አበባ',
        'promo': 'ልዩ ቅናሽ!',
        'sofa': 'ሶፋ',
        'bed': 'አልጋ',
        'mejlis': 'መጅሊስ',
        'curtain': 'መጋረጃ',
        'wardrobe': 'ቁምሳጥን',
        'admin_title': 'የአስተዳዳሪ ፓነል',
        'products_manage': 'ምርቶች',
        'add_product': 'ምርት ጨምር',
        'ads_manage': 'ማስታወቂያዎች',
        'cart': 'ጋሪ',
        'account': 'አካውንት',
        'checkout': 'ትዕዛዝ አስገባ',
        'total': 'ጠቅላላ',
        'subtotal': 'ንዑስ ድምር',
        'shipping': 'ማጓጓዝ',
        'free_shipping': 'ነጻ ማጓጓዝ',
        'full_name': 'ሙሉ ስም',
        'phone_number': 'ስልክ ቁጥር',
        'address_label': 'አድራሻ',
        'additional_notes': 'ተጨማሪ ማብራሪያ',
        'submit_order': 'ትዕዛዝ ላክ',
        'added_to_cart': 'ወደ ጋሪ ተጨምሯል',
        'password': 'የይለፍ ቃል',
        'confirm_password': 'የይለፍ ቃል አረጋግጥ',
        'no_account': 'አካውንት የለም',
        'have_account': 'አካውንት አለ',
        'edit_profile': 'መገለጫ አርትዕ',
    },
    'en': {
        # Search page
        'search_results': '🔍 Search Results',
        'find_perfect': 'Find the perfect furniture for your home',
        'showing_results_for': '🔎 Showing results for',
        'products_found': 'product(s) found',
        'no_products_found': 'No products found',
        'we_couldnt_find': "We couldn't find any products matching",
        'try_tips': '💡 Try:',
        'tip_different_keywords': '• Using different keywords',
        'tip_check_spelling': '• Checking the spelling',
        'tip_browse_categories': '• Browsing our categories',
        'back_to_home': '← Back to Home',
        'popular_searches': '💡 Popular searches:',
        'try_searching_for': '💡 Try searching for:',
        'sort_relevance': '📌 Relevance',
        'sort_price_low_high': '💰 Price: Low to High',
        'sort_price_high_low': '💰 Price: High to Low',
        'sort_name_az': '📝 Name: A to Z',
        'sort_name_za': '📝 Name: Z to A',
        'filter_products': '🔍 Filter Products',
        # Navigation & UI
        'search': 'Search products here...',
        'home': 'Home',
        'products': 'Products',
        'contact': 'Contact Us',
        'about_us': 'About Us',
        'our_branches': 'Our Branches',
        'all': 'All',
        'all_products': 'All Products',
        'categories': 'Categories',
        'featured_products': '⭐ Featured Products',
        'new_arrivals': '🆕 New Arrivals',
        'add_to_cart': '🛒 Add to Cart',
        'view_details': 'View Details →',
        'shop_now': 'Shop Now →',
        'login': 'Login',
        'register': 'Register',
        'logout': 'Logout',
        'profile': 'Profile',
        'my_orders': 'My Orders',
        'my_cart': 'My Cart',
        'in_stock': 'In Stock',
        'out_of_stock': 'Out of Stock',
        'quick_links': 'Quick Links',
        'call_us': 'Call Us',
        'quality_tagline': 'Quality furniture at affordable prices in Addis Ababa',
        'free_shipping_msg': '🚚 Free shipping on orders over 5,000 ETB',
        'copyright_text': 'All Rights Reserved',
        # Product form
        'order_now': 'Order Now',
        'about_us': 'About Us',
        'address': 'Address: Addis Ababa',
        'promo': 'Special Offer!',
        'sofa': 'Sofa',
        'bed': 'Bed',
        'mejlis': 'Mejlis',
        'curtain': 'Curtain',
        'wardrobe': 'Wardrobe',
        'admin_title': 'Admin Panel',
        'products_manage': 'Products',
        'add_product': 'Add Product',
        'ads_manage': 'Advertisements',
        'cart': 'Cart',
        'account': 'Account',
        'checkout': 'Checkout',
        'total': 'Total',
        'subtotal': 'Subtotal',
        'shipping': 'Shipping',
        'free_shipping': 'Free Shipping',
        'full_name': 'Full Name',
        'phone_number': 'Phone Number',
        'address_label': 'Address',
        'additional_notes': 'Additional Notes',
        'submit_order': 'Submit Order',
        'added_to_cart': 'Added to cart',
        'password': 'Password',
        'confirm_password': 'Confirm Password',
        'no_account': 'No account',
        'have_account': 'Have account',
        'edit_profile': 'Edit Profile',
    },
    'ar': {
        # Search page
        'search_results': '🔍 نتائج البحث',
        'find_perfect': 'ابحث عن الأثاث المثالي لمنزلك',
        'showing_results_for': '🔎 نتائج عن',
        'products_found': 'منتج وجد',
        'no_products_found': 'لم يتم العثور على منتجات',
        'we_couldnt_find': 'لم نجد أي منتجات تطابق',
        'try_tips': '💡 جرب:',
        'tip_different_keywords': '• استخدام كلمات مختلفة',
        'tip_check_spelling': '• التحقق من الإملاء',
        'tip_browse_categories': '• تصفح فئاتنا',
        'back_to_home': 'العودة للرئيسية →',
        'popular_searches': '💡 عمليات البحث الشائعة:',
        'try_searching_for': '💡 جرب البحث عن:',
        'sort_relevance': '📌 الصلة',
        'sort_price_low_high': '💰 السعر: من الأدنى',
        'sort_price_high_low': '💰 السعر: من الأعلى',
        'sort_name_az': '📝 الاسم: أ-ي',
        'sort_name_za': '📝 الاسم: ي-أ',
        'filter_products': '🔍 تصفية المنتجات',
        # Navigation & UI
        'search': 'ابحث عن المنتجات هنا...',
        'home': 'الرئيسية',
        'products': 'المنتجات',
        'contact': 'اتصل بنا',
        'about_us': 'معلومات عنا',
        'our_branches': 'فروعنا',
        'all': 'الجميع',
        'all_products': 'جميع المنتجات',
        'categories': 'الفئات',
        'featured_products': '⭐ المنتجات المميزة',
        'new_arrivals': '🆕 وصل حديثاً',
        'add_to_cart': '🛒 أضف إلى السلة',
        'view_details': 'عرض التفاصيل ←',
        'shop_now': 'تسوق الآن ←',
        'login': 'تسجيل الدخول',
        'register': 'تسجيل',
        'logout': 'تسجيل الخروج',
        'profile': 'الملف الشخصي',
        'my_orders': 'طلباتي',
        'my_cart': 'سلتي',
        'in_stock': 'متوفر',
        'out_of_stock': 'غير متوفر',
        'quick_links': 'روابط سريعة',
        'call_us': 'اتصل بنا',
        'quality_tagline': 'أثاث عالي الجودة بأسعار معقولة في أديس أبابا',
        'free_shipping_msg': '🚚 شحن مجاني للطلبات التي تتجاوز 5,000 بر إثيوبي',
        'copyright_text': 'جميع الحقوق محفوظة',
        # Product form
        'order_now': 'اطلب الآن',
        'address': 'العنوان: أديس أبابا',
        'promo': 'عرض خاص!',
        'sofa': 'أريكة',
        'bed': 'سرير',
        'mejlis': 'مجلس',
        'curtain': 'ستارة',
        'wardrobe': 'خزانة',
        'cart': 'السلة',
        'account': 'الحساب',
        'checkout': 'الدفع',
        'total': 'المجموع',
        'subtotal': 'المجموع الفرعي',
        'shipping': 'الشحن',
        'free_shipping': 'شحن مجاني',
        'full_name': 'الاسم الكامل',
        'phone_number': 'رقم الهاتف',
        'address_label': 'العنوان',
        'additional_notes': 'ملاحظات إضافية',
        'submit_order': 'تقديم الطلب',
        'added_to_cart': 'تمت الإضافة إلى السلة',
        'password': 'كلمة المرور',
        'confirm_password': 'تأكيد كلمة المرور',
        'no_account': 'لا يوجد حساب',
        'have_account': 'لديك حساب',
        'edit_profile': 'تعديل الملف الشخصي',
    }
}

DEFAULT_LANGUAGE = 'am'
SUPPORTED_LANGUAGES = ['am', 'en', 'ar']


def get_lang():
    lang = session.get('lang', DEFAULT_LANGUAGE)
    return lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def get_text(key, lang=None):
    """
    Get translated text for a key.
    Tries dynamic translation first, falls back to manual TEXTS dictionary.
    
    Args:
        key (str): Text key or English text to translate
        lang (str): Language code ('am', 'en', 'ar')
    
    Returns:
        str: Translated text
    """
    if lang is None:
        lang = get_lang()
    
    # If requesting English, return the key as-is
    if lang == 'en':
        return key
    
    # First, check if we have a manual translation
    manual_translation = TEXTS.get(lang, {}).get(key)
    if manual_translation:
        return manual_translation
    
    # For items not in manual TEXTS, try dynamic translation
    try:
        translated = translate_text(key, lang)
        return translated
    except Exception as e:
        app.logger.debug(f"Dynamic translation failed for '{key}': {str(e)}")
        # Fall back to original text
        return key


def set_lang(lang):
    if lang in SUPPORTED_LANGUAGES:
        session['lang'] = lang
        return True
    return False


@app.context_processor
def inject_language():
    current_lang = get_lang()

    def _(key):
        return get_text(key, current_lang)

    def product_name(product, lang=None):
        lng = lang or current_lang
        try:
            if lng == 'am':
                return product['name_am'] or product['name'] or ''
            elif lng == 'ar':
                return product['name_ar'] or product['name'] or ''
            else:
                return product['name_en'] or product['name'] or ''
        except (KeyError, TypeError):
            return ''

    def product_description(product, lang=None):
        lng = lang or current_lang
        try:
            if lng == 'am':
                return product['description_am'] or product['description'] or ''
            elif lng == 'ar':
                return product['description_ar'] or product['description'] or ''
            else:
                return product['description_en'] or product['description'] or ''
        except (KeyError, TypeError):
            return ''

    def localized_ad_title(ad, lang=None):
        lng = lang or current_lang
        try:
            if lng == 'am':
                return ad['title_am'] or ad['title'] or ''
            elif lng == 'ar':
                return ad['title_ar'] or ad['title'] or ''
            else:
                return ad['title'] or ''
        except (KeyError, TypeError):
            return ''

    def localized_ad_description(ad, lang=None):
        lng = lang or current_lang
        try:
            if lng == 'am':
                return ad['description_am'] or ad['description'] or ''
            elif lng == 'ar':
                return ad['description_ar'] or ad['description'] or ''
            else:
                return ad['description'] or ''
        except (KeyError, TypeError):
            return ''

    def category_name(cat, lang=None):
        lng = lang or current_lang
        try:
            if lng == 'am':
                return cat['name_am'] or cat['name'] or ''
            elif lng == 'ar':
                return cat['name_ar'] or cat['name'] or ''
            else:
                return cat['name'] or ''
        except (KeyError, TypeError):
            return ''

    return {
        'lang': current_lang,
        't': TEXTS.get(current_lang, TEXTS[DEFAULT_LANGUAGE]),
        '_': _,
        'product_name': product_name,
        'product_description': product_description,
        'localized_ad_title': localized_ad_title,
        'localized_ad_description': localized_ad_description,
        'category_name': category_name,
    }


@app.route('/set_lang/<lang>')
def set_language(lang):
    set_lang(lang)
    next_page = request.referrer
    if next_page and next_page != request.url:
        return redirect(next_page)
    return redirect(url_for('index'))


# ==================== 6. HELPER FUNCTIONS ====================

def format_price_helper(price):
    try:
        price_float = float(price)
        return f"{price_float:,.0f} ETB"
    except (ValueError, TypeError):
        return f"{price} ETB"


def _format_price_number(price):
    try:
        price_float = float(price)
        return f"{price_float:,.0f}"
    except (ValueError, TypeError):
        return str(price)


@app.template_filter('format_price')
def format_price_filter(price):
    return format_price_helper(price)


@app.template_filter('format_number')
def format_number_filter(price):
    return _format_price_number(price)


# ==================== 7. ERROR HANDLERS ====================

@app.errorhandler(404)
def page_not_found(error):
    """Handle 404 - Page Not Found errors."""
    app.logger.warning(f"404 Error: {request.url}")
    lang = get_lang()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': False, 'error': 'Page not found'}), 404

    titles = {
        'am': 'ገጹ አልተገኘም',
        'en': 'Page Not Found',
        'ar': 'الصفحة غير موجودة'
    }
    messages = {
        'am': 'ይቅርታ፣ እርስዎ የፈለጉት ገጽ የለም።',
        'en': 'Sorry, the page you are looking for does not exist.',
        'ar': 'عذرًا، الصفحة التي تبحث عنها غير موجودة.'
    }

    title = titles.get(lang, titles['en'])
    message = messages.get(lang, messages['en'])

    return f'''
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><title>404 - {title}</title>
    <style>
        *{{margin:0;padding:0;box-sizing:border-box;}}
        body{{font-family:Arial, sans-serif;background:linear-gradient(135deg,#1a73e8,#0d47a1);display:flex;justify-content:center;align-items:center;height:100vh;}}
        .box{{background:white;padding:40px;border-radius:20px;text-align:center;max-width:400px;}}
        h1{{color:#1a73e8;font-size:80px;margin:0;}}
        p{{margin:20px 0;color:#666;}}
        a{{display:inline-block;background:#1a73e8;color:white;padding:12px 24px;text-decoration:none;border-radius:30px;}}
    </style>
    </head>
    <body><div class="box"><h1>404</h1><p>{message}</p><a href="/">Home</a></div></body>
    </html>
    ''', 404


@app.errorhandler(500)
def internal_server_error(error):
    """Handle 500 - Internal Server Error."""
    app.logger.error(f"500 Error: {request.url} - {error}")
    import traceback
    app.logger.error(traceback.format_exc())
    lang = get_lang()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

    messages = {
        'am': 'ችግር ተፈጥሯል። እባክዎ ቆይተው ይሞክሩ።',
        'en': 'Something went wrong. Please try again later.',
        'ar': 'حدث خطأ ما. يرجى المحاولة مرة أخرى لاحقًا.'
    }
    message = messages.get(lang, messages['en'])

    return f'''
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><title>500 - Server Error</title>
    <style>
        *{{margin:0;padding:0;box-sizing:border-box;}}
        body{{font-family:Arial,sans-serif;background:linear-gradient(135deg,#e44d26,#c0392b);display:flex;justify-content:center;align-items:center;height:100vh;}}
        .box{{background:white;padding:40px;border-radius:20px;text-align:center;max-width:400px;}}
        h1{{color:#e44d26;font-size:80px;margin:0;}}
        p{{margin:20px 0;color:#666;}}
        a{{display:inline-block;background:#e44d26;color:white;padding:12px 24px;text-decoration:none;border-radius:30px;}}
    </style>
    </head>
    <body><div class="box"><h1>500</h1><p>{message}</p><a href="/">Home</a></div></body>
    </html>
    ''', 500


@app.errorhandler(403)
def forbidden_error(error):
    """Handle 403 - Forbidden Error."""
    app.logger.warning(f"403 Error: {request.url}")
    lang = get_lang()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': False, 'error': 'Forbidden'}), 403

    messages = {
        'am': 'ይህን ገጽ ለማየት ፈቃድ የለዎትም።',
        'en': 'You do not have permission to access this page.',
        'ar': 'ليس لديك إذن للوصول إلى هذه الصفحة.'
    }
    message = messages.get(lang, messages['en'])

    return f'''
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><title>403 - Forbidden</title>
    <style>
        *{{margin:0;padding:0;box-sizing:border-box;}}
        body{{font-family:Arial, sans-serif;background:linear-gradient(135deg,#f39c12,#e67e22);display:flex;justify-content:center;align-items:center;height:100vh;}}
        .box{{background:white;padding:40px;border-radius:20px;text-align:center;max-width:400px;}}
        h1{{color:#f39c12;font-size:80px;margin:0;}}
        p{{margin:20px 0;color:#666;}}
        .buttons{{display:flex;gap:10px;justify-content:center;}}
        a{{display:inline-block;padding:10px 20px;border-radius:30px;text-decoration:none;}}
        .home{{background:#1a73e8;color:white;}}
        .login{{background:#f39c12;color:white;}}
    </style>
    </head>
    <body><div class="box"><h1>403</h1><p>{message}</p><div class="buttons"><a href="/" class="home">Home</a><a href="/login" class="login">Login</a></div></div></body>
    </html>
    ''', 403


# ==================== 8. SECURITY HEADERS ====================

@app.after_request
def add_security_headers(response):
    """Add security headers to all responses."""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    return response


@app.before_request
def log_request_info():
    """Log incoming requests for debugging."""
    if request.method in ['POST', 'PUT', 'DELETE']:
        app.logger.debug(f"Incoming {request.method} {request.path} - IP: {request.remote_addr}")
    request.start_time = time.time()


@app.after_request
def log_response_info(response):
    """Log response time for debugging slow requests."""
    if hasattr(request, 'start_time'):
        elapsed = time.time() - request.start_time
        if elapsed > 1.0:
            app.logger.warning(f"Slow request: {request.method} {request.path} - {elapsed:.2f}s")
    return response


# ==================== 9. HEALTH CHECK ====================

@app.route('/health')
def health_check():
    """Health check endpoint for monitoring services."""
    health_status = {
        'status': 'healthy',
        'timestamp': datetime_.datetime.now().isoformat(),
        'version': '2.0.0',
        'services': {}
    }

    try:
        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT 1")
        health_status['services']['database'] = 'connected'
    except Exception:
        health_status['services']['database'] = 'error'
        health_status['status'] = 'unhealthy'

    try:
        if os.path.exists(app.config['UPLOAD_FOLDER']):
            health_status['services']['storage'] = 'accessible'
        else:
            health_status['services']['storage'] = 'not_found'
    except Exception:
        health_status['services']['storage'] = 'error'

    status_code = 200 if health_status['status'] == 'healthy' else 503
    return jsonify(health_status), status_code


@app.route('/fix-everything')
def fix_everything():
    """Test route: ensures categories exist and returns a status report."""
    import sqlite3 as _sqlite3
    from config import Config as _Config
    db_path = _Config.DATABASE_PATH
    conn = _sqlite3.connect(db_path)
    conn.row_factory = _sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM categories")
    before = cur.fetchone()[0]

    inserted = 0
    if before == 0:
        defaults = [
            ('Living Room', 'ሳሎን',      'غرفة المعيشة', '🛋️', 1),
            ('Bedroom',     'መኝታ ክፍል',  'غرفة النوم',   '🛏️', 2),
            ('Office',      'ቢሮ',        'مكتب',          '💼',  3),
            ('Dining',      'መመገቢያ',    'غرفة الطعام',  '🍽️', 4),
        ]
        cur.executemany(
            "INSERT INTO categories (name, name_am, name_ar, icon, sort_order) VALUES (?, ?, ?, ?, ?)",
            defaults
        )
        conn.commit()
        inserted = len(defaults)

    cur.execute("SELECT COUNT(*) FROM categories")
    after = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM products")
    products_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM orders")
    orders_count = cur.fetchone()[0]
    conn.close()

    return jsonify({
        'status': 'ok',
        'categories_before': before,
        'categories_inserted': inserted,
        'categories_after': after,
        'products': products_count,
        'orders': orders_count,
        'message': 'All fixes applied successfully!' if inserted else 'Categories already present — no changes needed.'
    })


# ==================== 10. CUSTOMER ROUTES ====================

@app.route('/')
def index():
    """Home page - displays products and advertisements with platform detection."""
    lang = get_lang()
    conn = None
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get featured products (is_featured = 1)
        cursor.execute("""
            SELECT p.*, c.name as category_name, c.name_am as category_name_am
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.is_active = 1 AND p.is_featured = 1
            ORDER BY p.id DESC
            LIMIT 12
        """)
        featured_products = cursor.fetchall()
        
        # Get new products (is_new = 1)
        cursor.execute("""
            SELECT p.*, c.name as category_name, c.name_am as category_name_am
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.is_active = 1 AND p.is_new = 1
            ORDER BY p.id DESC
            LIMIT 12
        """)
        new_products = cursor.fetchall()
        
        # Get active advertisements for slider
        cursor.execute("""
            SELECT * FROM advertisements 
            WHERE is_active = 1 
            AND (end_date IS NULL OR end_date > NOW())
            AND (start_date IS NULL OR start_date <= NOW())
            ORDER BY sort_order ASC, id DESC
        """)
        ads = cursor.fetchall()
        
        # Get all active categories
        cursor.execute("""
            SELECT * FROM categories 
            WHERE is_active = 1 
            ORDER BY sort_order ASC
        """)
        categories = cursor.fetchall()
        

        
        # Convert to dict for template
        def row_to_dict(row):
            if row is None:
                return {}
            return {key: row[key] for key in row.keys()}
        
        featured_list = [row_to_dict(p) for p in featured_products] if featured_products else []
        new_list = [row_to_dict(p) for p in new_products] if new_products else []
        ads_list = [row_to_dict(ad) for ad in ads] if ads else []
        categories_list = [row_to_dict(cat) for cat in categories] if categories else []
        
        # Platform detection for showing/hiding about section
        platform = get_platform()
        show_about = platform == 'desktop' or platform == 'mobile_browser'
        
        app.logger.info(f"Home page - Featured: {len(featured_list)}, New: {len(new_list)}, Ads: {len(ads_list)}")
        
        return render_template('customer/index.html', 
                               featured_products=featured_list,
                               new_products=new_list,
                               ads=ads_list,
                               categories=categories_list,
                               show_about=show_about,
                               platform=platform,
                               lang=lang,
                               t=TEXTS.get(lang, TEXTS['am']))
                               
    except Exception as e:
        app.logger.error(f"Home page error: {str(e)}")
        import traceback
        app.logger.error(traceback.format_exc())

        return render_template('customer/index.html', 
                               featured_products=[], 
                               new_products=[],
                               ads=[], 
                               categories=[],
                               show_about=True,
                               platform='desktop',
                               lang=lang, 
                               t=TEXTS.get(lang, TEXTS['am']))

@app.route('/product/<int:product_id>')
def product_detail(product_id):
    """Product detail page with related products."""
    lang = get_lang()
    conn = None
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # 1. የርዕሱን መረጃ ማምጣት
        cursor.execute("""
            SELECT p.*, c.name as category_name, c.name_am as category_name_am
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.id = ? AND p.is_active = 1
        """, (product_id,))
        
        product = cursor.fetchone()
        
        if not product:
            flash('Product not found', 'error')
            return redirect(url_for('index'))
        
        # 2. ተዛማጅ ምርቶችን ማምጣት
        cursor.execute("""
            SELECT p.*, c.name as category_name
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.category_id = ? AND p.id != ? AND p.is_active = 1
            ORDER BY p.id DESC
            LIMIT 4
        """, (product['category_id'], product_id))
        
        related_products = cursor.fetchall()
        
        # 3. የዕይታ ብዛት መጨመር (ይህ ቀድሞ ስህተት የነበረበት ቦታ ነው)
        cursor.execute("UPDATE products SET views = views + 1 WHERE id = ?", (product_id,))
        conn.commit()

        # 4. መረጃዎችን ወደ ዲክሽነሪ መቀየር
        product_dict = dict(product)
        related_list = [dict(p) for p in related_products] if related_products else []
        
        # ዳታቤዙን እዚህ ጋር እንዘጋዋለን

        
        # የተቀረው የኮድህ ክፍል (Price calculation)
        if product_dict.get('thumbnail') is None or str(product_dict.get('thumbnail')) == 'None':
            product_dict['thumbnail'] = ''
        
        android_user = is_android_app()
        is_logged_in = session.get('user_id') is not None
        final_price = product_dict['price']
        if is_logged_in and android_user:
            final_price = product_dict['price'] * 0.9
        
        return render_template('customer/product_detail.html', 
                               product=product_dict,
                               related_products=related_list,
                               final_price=round(final_price, 2),
                               is_logged_in=is_logged_in,
                               lang=lang, 
                               t=TEXTS.get(lang, TEXTS['am']))
                               
    except Exception as e:
        app.logger.error(f"Product detail error: {str(e)}")

        flash('Unable to load product', 'error')
        return redirect(url_for('index'))

@app.route('/category')
@app.route('/category/<int:category_id>')
def category_products(category_id=None):
    """Category products page."""
    lang = get_lang()
    safe_t = TEXTS.get(lang, TEXTS.get('am', {}))
    conn = None
    
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get all categories for filter
        cursor.execute("SELECT * FROM categories WHERE is_active = 1 ORDER BY sort_order ASC")
        categories = cursor.fetchall()
        
        category_name = None
        if category_id:
            cursor.execute("SELECT * FROM categories WHERE id = ? AND is_active = 1", (category_id,))
            category = cursor.fetchone()
            if category:
                category_name = category['name_am'] if lang == 'am' else category['name']
                cursor.execute("""
                    SELECT p.*, c.name as category_name
                    FROM products p
                    LEFT JOIN categories c ON p.category_id = c.id
                    WHERE p.category_id = ? AND p.is_active = 1
                    ORDER BY p.id DESC
                """, (category_id,))
            else:
                category_name = 'All Products' if lang == 'en' else 'ሁሉም ምርቶች'
                cursor.execute("""
                    SELECT p.*, c.name as category_name
                    FROM products p
                    LEFT JOIN categories c ON p.category_id = c.id
                    WHERE p.is_active = 1
                    ORDER BY p.id DESC
                """)
        else:
            category_name = 'All Products' if lang == 'en' else 'ሁሉም ምርቶች'
            cursor.execute("""
                SELECT p.*, c.name as category_name
                FROM products p
                LEFT JOIN categories c ON p.category_id = c.id
                WHERE p.is_active = 1
                ORDER BY p.id DESC
            """)
        
        products = cursor.fetchall()

        
        products_list = [dict(p) for p in products] if products else []
        categories_list = [dict(cat) for cat in categories] if categories else []
        
        app.logger.info(f"Category '{category_name}' loaded - {len(products_list)} products")
        
        return render_template('customer/category.html',
                               products=products_list,
                               categories=categories_list,
                               category_name=category_name,
                               current_category=category_id,
                               lang=lang,
                               t=safe_t)
                               
    except Exception as e:
        app.logger.error(f"Category error: {str(e)}")
        import traceback
        app.logger.error(traceback.format_exc())

        flash('Unable to load category products. Please try again.', 'error')
        return render_template('customer/category.html',
                               products=[],
                               categories=[],
                               category_name='Products',
                               current_category=None,
                               lang=lang,
                               t=safe_t)


@app.route('/products')
def products():
    """All products page with grid layout."""
    lang = get_lang()
    page = request.args.get('page', 1, type=int)
    per_page = 12
    conn = None
    
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get total count
        cursor.execute("SELECT COUNT(*) FROM products WHERE is_active = 1")
        total = cursor.fetchone()[0]
        
        # Get products with pagination
        offset = (page - 1) * per_page
        cursor.execute("""
            SELECT p.*, c.name as category_name, c.name_am as category_name_am
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.is_active = 1
            ORDER BY p.id DESC
            LIMIT ? OFFSET ?
        """, (per_page, offset))
        
        products = cursor.fetchall()
        
        # Get all categories for filter
        cursor.execute("SELECT * FROM categories WHERE is_active = 1 ORDER BY sort_order ASC")
        categories = cursor.fetchall()
        

        
        products_list = [dict(p) for p in products] if products else []
        categories_list = [dict(cat) for cat in categories] if categories else []
        
        total_pages = (total + per_page - 1) // per_page
        
        return render_template('customer/product_grid.html',
                               products=products_list,
                               categories=categories_list,
                               page=page,
                               total_pages=total_pages,
                               total=total,
                               lang=lang,
                               t=TEXTS.get(lang, TEXTS['am']))
                               
    except Exception as e:
        app.logger.error(f"Products page error: {str(e)}")

        return render_template('customer/product_grid.html',
                               products=[],
                               categories=[],
                               page=1,
                               total_pages=0,
                               total=0,
                               lang=lang,
                               t=TEXTS.get(lang, TEXTS['am']))


@app.route('/search')
def search():
    """Search products page."""
    lang = get_lang()
    query = request.args.get('q', '').strip()
    conn = None
    
    if not query:
        return redirect(url_for('products'))
    
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        search_pattern = f'%{query}%'
        cursor.execute("""
            SELECT p.*, c.name as category_name, c.name_am as category_name_am, c.name_ar as category_name_ar
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.is_active = 1
            AND (
                p.name LIKE ? OR p.name_en LIKE ? OR p.name_am LIKE ? OR p.name_ar LIKE ?
                OR p.description LIKE ? OR p.description_en LIKE ?
                OR p.description_am LIKE ? OR p.description_ar LIKE ?
            )
            ORDER BY
                CASE WHEN p.name_am LIKE ? OR p.name_en LIKE ? OR p.name_ar LIKE ? THEN 0 ELSE 1 END,
                p.is_featured DESC,
                p.id DESC
        """, (
            search_pattern, search_pattern, search_pattern, search_pattern,
            search_pattern, search_pattern, search_pattern, search_pattern,
            search_pattern, search_pattern, search_pattern
        ))
        
        products = cursor.fetchall()
        
        # Get categories for suggestion
        cursor.execute("SELECT * FROM categories WHERE is_active = 1 ORDER BY sort_order ASC LIMIT 6")
        categories = cursor.fetchall()
        

        
        products_list = [dict(p) for p in products] if products else []
        categories_list = [dict(cat) for cat in categories] if categories else []
        
        app.logger.info(f"Search '{query}' found {len(products_list)} results")
        
        return render_template('customer/search.html', 
                               products=products_list,
                               categories=categories_list,
                               query=query, 
                               lang=lang, 
                               t=TEXTS.get(lang, TEXTS['am']))
                               
    except Exception as e:
        app.logger.error(f"Search error: {str(e)}")
        import traceback
        app.logger.error(traceback.format_exc())

        flash('Search failed. Please try again.', 'error')
        return render_template('customer/search.html', 
                               products=[], 
                               categories=[],
                               query=query, 
                               lang=lang, 
                               t=TEXTS.get(lang, TEXTS['am']))


@app.route('/about')
def about():
    """About page - shows company information."""
    lang = get_lang()
    platform = get_platform()
    
    # On Android app, about section is hidden, but this page is still accessible
    return render_template('customer/about.html', 
                           lang=lang, 
                           platform=platform,
                           t=TEXTS.get(lang, TEXTS['am']))


@app.route('/contact', methods=['GET', 'POST'])
def contact():
    """Contact page with WhatsApp integration and form submission."""
    lang = get_lang()
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        message = request.form.get('message', '').strip()
        
        if not name or not message:
            flash('Please fill in name and message', 'error')
            return redirect(url_for('contact'))
        
        # Save to database
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS contacts (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    email TEXT,
                    phone TEXT,
                    message TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cursor.execute("""
                INSERT INTO contacts (name, email, phone, message)
                VALUES (?, ?, ?, ?)
            """, (name, email, phone, message))
            conn.commit()

        except Exception as e:
            app.logger.error(f"Error saving contact: {str(e)}")
        
        # Create WhatsApp message
        whatsapp_msg = f"📬 New Contact Message - Ethiosadat Furniture\n\n"
        whatsapp_msg += f"👤 Name: {name}\n"
        if email:
            whatsapp_msg += f"📧 Email: {email}\n"
        if phone:
            whatsapp_msg += f"📞 Phone: {phone}\n"
        whatsapp_msg += f"\n💬 Message:\n{message}\n\n"
        whatsapp_msg += f"Sent from Ethiosadat Furniture website"
        
        encoded = urllib.parse.quote(whatsapp_msg)
        whatsapp_url = f"https://wa.me/{WHATSAPP_NUMBER}?text={encoded}"
        
        flash('Message sent! You will be redirected to WhatsApp.', 'success')
        return redirect(whatsapp_url)
    
    # Get branches for contact page
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM branches 
        WHERE is_active = 1 
        ORDER BY sort_order ASC
    """)
    branches = cursor.fetchall()
    
    branches_list = [dict(branch) for branch in branches] if branches else []
    
    return render_template('customer/contact.html', 
                           lang=lang, 
                           branches=branches_list,
                           whatsapp_number=WHATSAPP_NUMBER,
                           t=TEXTS.get(lang, TEXTS['am']))


@app.route('/branches')
def branches():
    """Branches page with Google Maps integration."""
    lang = get_lang()
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT * FROM branches 
        WHERE is_active = 1 
        ORDER BY sort_order ASC
    """)
    
    branches = cursor.fetchall()
    
    branches_list = []
    for branch in branches:
        branch_dict = dict(branch)
        # Generate Google Maps URL for each branch
        branch_dict['maps_url'] = f"https://www.google.com/maps/dir/?api=1&destination={branch_dict['latitude']},{branch_dict['longitude']}"
        branches_list.append(branch_dict)
    
    phone_numbers = [WHATSAPP_NUMBER, '251906080606', '251906090606']
    
    return render_template('customer/branches.html',
                           branches=branches_list,
                           phone_numbers=phone_numbers,
                           lang=lang,
                           t=TEXTS.get(lang, TEXTS['am']))


@app.route('/faq')
def faq():
    """Frequently Asked Questions page."""
    lang = get_lang()
    return render_template('customer/faq.html', 
                           lang=lang, 
                           t=TEXTS.get(lang, TEXTS['am']))


@app.route('/shipping-info')
def shipping_info():
    """Shipping information page."""
    lang = get_lang()
    return render_template('customer/shipping_info.html', 
                           lang=lang, 
                           t=TEXTS.get(lang, TEXTS['am']))


@app.route('/returns')
def returns_policy():
    """Returns policy page."""
    lang = get_lang()
    return render_template('customer/returns.html', 
                           lang=lang, 
                           t=TEXTS.get(lang, TEXTS['am']))


@app.route('/terms')
def terms():
    """Terms and Conditions page."""
    lang = get_lang()
    return render_template('customer/terms.html', 
                           lang=lang, 
                           t=TEXTS.get(lang, TEXTS['am']))


@app.route('/privacy')
def privacy():
    """Privacy Policy page."""
    lang = get_lang()
    return render_template('customer/privacy.html', 
                           lang=lang, 
                           t=TEXTS.get(lang, TEXTS['am']))


# ==================== 11. STATIC FILE ROUTES ====================

@app.route('/static/uploads/<path:filename>')
def serve_upload(filename):
    """Serve uploaded files securely."""
    if '..' in filename or filename.startswith('/'):
        abort(404)
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/static/uploads/products/<path:filename>')
def uploaded_product_image(filename):
    """Serve product images with cache headers."""
    try:
        if '..' in filename or filename.startswith('/'):
            abort(404)
        
        response = send_from_directory(
            os.path.join(app.config['UPLOAD_FOLDER'], 'products'),
            filename
        )
        response.headers['Cache-Control'] = 'public, max-age=86400'
        return response
        
    except FileNotFoundError:
        return send_from_directory('static/images', 'placeholder.png')
    except Exception as e:
        app.logger.error(f"Error serving product image {filename}: {str(e)}")
        abort(404)


@app.route('/static/uploads/ads/<path:filename>')
def uploaded_ad_media(filename):
    """Serve advertisement media files."""
    try:
        if '..' in filename or filename.startswith('/'):
            abort(404)
        
        response = send_from_directory(
            os.path.join(app.config['UPLOAD_FOLDER'], 'ads'),
            filename
        )
        response.headers['Cache-Control'] = 'public, max-age=43200'
        return response
        
    except FileNotFoundError:
        app.logger.warning(f"Ad media not found: {filename}")
        abort(404)
    except Exception as e:
        app.logger.error(f"Error serving ad media {filename}: {str(e)}")
        abort(500)


@app.route('/favicon.ico')
def favicon():
    """Serve the website favicon."""
    try:
        return send_from_directory(
            os.path.join(app.root_path, 'static'),
            'favicon.ico',
            mimetype='image/vnd.microsoft.icon'
        )
    except FileNotFoundError:
        return '', 204


@app.route('/static/images/<path:filename>')
def static_image(filename):
    """Serve static images with cache optimization."""
    try:
        if '..' in filename or filename.startswith('/'):
            abort(404)
        
        response = send_from_directory('static/images', filename)
        response.headers['Cache-Control'] = 'public, max-age=604800'
        return response
        
    except FileNotFoundError:
        return send_from_directory('static/images', 'placeholder.png')
    except Exception as e:
        app.logger.error(f"Error serving static image {filename}: {str(e)}")
        abort(404)


@app.route('/static/css/<path:filename>')
def static_css(filename):
    """Serve CSS files with cache headers."""
    try:
        if '..' in filename:
            abort(404)
        
        response = send_from_directory('static/css', filename)
        response.headers['Cache-Control'] = 'public, max-age=3600'
        response.headers['Content-Type'] = 'text/css'
        return response
        
    except FileNotFoundError:
        abort(404)
    except Exception as e:
        app.logger.error(f"Error serving CSS {filename}: {str(e)}")
        abort(500)


@app.route('/static/js/<path:filename>')
def static_js(filename):
    """Serve JavaScript files with cache headers."""
    try:
        if '..' in filename:
            abort(404)
        
        response = send_from_directory('static/js', filename)
        response.headers['Cache-Control'] = 'public, max-age=3600'
        response.headers['Content-Type'] = 'application/javascript'
        return response
        
    except FileNotFoundError:
        abort(404)
    except Exception as e:
        app.logger.error(f"Error serving JS {filename}: {str(e)}")
        abort(500)
# ==================== 12. USER AUTHENTICATION ROUTES ====================

@app.route('/login/user', methods=['GET', 'POST'])
def user_login():
    """User login page with session management."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    # If already logged in, redirect to profile
    if session.get('user_id'):
        flash('You are already logged in!', 'info')
        return redirect(url_for('user_profile'))
    
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        
        if not email or not password:
            if is_ajax:
                return jsonify({'success': False, 'error': 'Please enter both email and password'})
            flash('Please enter both email and password', 'error')
            return render_template('auth/user_login.html', lang=lang, t=t)
        
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE email = ? AND is_active = 1", (email,))
            user = cursor.fetchone()
            
            if user and check_password_hash(user['password_hash'], password):
                session['user_id'] = user['id']
                session['user_name'] = user['full_name']
                session['user_email'] = user['email']
                session['user_phone'] = user['phone']
                session.permanent = True
                
                app.logger.info(f"User logged in: {email}")
                
                merge_guest_cart(user['id'])
                
                if is_ajax:
                    return jsonify({'success': True, 'message': f'Welcome back, {user["full_name"]}!'})
                
                flash(f'Welcome back, {user["full_name"]}!', 'success')
                next_page = request.args.get('next')
                if next_page and next_page.startswith('/'):
                    return redirect(next_page)
                return redirect(url_for('index'))
            else:
                app.logger.warning(f"Failed login attempt for email: {email}")
                if is_ajax:
                    return jsonify({'success': False, 'error': 'Invalid email or password'})
                flash('Invalid email or password', 'error')
                
        except Exception as e:
            app.logger.error(f"Login error: {str(e)}")
            if is_ajax:
                return jsonify({'success': False, 'error': 'Login failed. Please try again.'})
            flash('Login failed. Please try again.', 'error')
    
    return render_template('auth/user_login.html', lang=lang, t=t)


def merge_guest_cart(user_id):
    """Merge guest session cart with user's database cart."""
    guest_cart = session.get('cart', {})
    if not guest_cart:
        return
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        for product_id, quantity in guest_cart.items():
            cursor.execute(
                "SELECT id, quantity FROM cart_items WHERE user_id = ? AND product_id = ?",
                (user_id, product_id)
            )
            existing = cursor.fetchone()
            
            if existing:
                new_quantity = existing[1] + quantity
                cursor.execute(
                    "UPDATE cart_items SET quantity = ? WHERE id = ?",
                    (new_quantity, existing[0])
                )
            else:
                cursor.execute(
                    "INSERT INTO cart_items (user_id, product_id, quantity) VALUES (?, ?, ?)",
                    (user_id, product_id, quantity)
                )
        
        conn.commit()

        
        # Clear session cart
        session.pop('cart', None)
        session.modified = True
        
        app.logger.info(f"Merged guest cart with user {user_id}")
        
    except Exception as e:
        app.logger.error(f"Error merging guest cart: {str(e)}")


@app.route('/register', methods=['GET', 'POST'])
def user_register():
    """User registration page."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    # If already logged in, redirect to profile
    if session.get('user_id'):
        return redirect(url_for('user_profile'))
    
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        # Validation
        errors = []
        if not full_name:
            errors.append('Full name is required')
        if not email:
            errors.append('Email is required')
        elif not re.match(r'^[^@]+@([^@.,]+\.)+[^@.,]{2,}$', email):
            errors.append('Invalid email address')
        if not password:
            errors.append('Password is required')
        elif len(password) < 6:
            errors.append('Password must be at least 6 characters')
        if password != confirm_password:
            errors.append('Passwords do not match')
        
        if errors:
            for error in errors:
                flash(error, 'error')
            return render_template('auth/user_register.html', lang=lang, t=t)
        
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        try:
            conn = get_db()
            cursor = conn.cursor()
            
            # Check if email exists
            cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
            if cursor.fetchone():
                if is_ajax:
                    return jsonify({'success': False, 'error': 'Email already registered. Please login.'})
                flash('Email already registered. Please login.', 'error')
                return render_template('auth/user_register.html', lang=lang, t=t)
            
            # Derive a unique username from email
            username_base = email.split('@')[0].lower()
            username = username_base
            # Ensure uniqueness
            suffix = 1
            while True:
                cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
                if not cursor.fetchone():
                    break
                username = f"{username_base}{suffix}"
                suffix += 1
            
            # Create user
            password_hash = generate_password_hash(password, method='pbkdf2:sha256')
            cursor.execute("""
                INSERT INTO users (username, full_name, email, phone, password_hash, is_admin, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, 0, 1, CURRENT_TIMESTAMP)
            """, (username, full_name, email, phone, password_hash))
            conn.commit()
            
            # Get the user_id from the new user (fetch from database)
            cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
            user_row = cursor.fetchone()
            user_id = user_row['id'] if user_row else None
            
            # Auto login
            session['user_id'] = user_id
            session['user_name'] = full_name
            session['user_email'] = email
            session['user_phone'] = phone
            session.permanent = True
            
            app.logger.info(f"New user registered: {email}")
            
            # Send welcome email (optional)
            send_welcome_email(email, full_name)
            
            # Merge guest cart
            merge_guest_cart(user_id)
            
            if is_ajax:
                return jsonify({'success': True, 'message': 'Registration successful! Welcome to Ethiosadat Furniture!'})
            
            flash('Registration successful! Welcome to Ethiosadat Furniture!', 'success')
            return redirect(url_for('index'))
            
        except Exception as e:
            app.logger.error(f"Registration error: {str(e)}")
            if is_ajax:
                return jsonify({'success': False, 'error': 'Registration failed. Please try again.'})
            flash('Registration failed. Please try again.', 'error')
    
    return render_template('auth/user_register.html', lang=lang, t=t)


def send_welcome_email(email, name):
    """Send welcome email to new user."""
    try:
        # This is a placeholder - implement actual email sending
        app.logger.info(f"Welcome email would be sent to {email}")
    except Exception as e:
        app.logger.error(f"Error sending welcome email: {str(e)}")


@app.route('/logout/user')
def user_logout():
    """User logout - clear session."""
    user_name = session.get('user_name', 'User')
    session.pop('user_id', None)
    session.pop('user_name', None)
    session.pop('user_email', None)
    session.pop('user_phone', None)
    
    app.logger.info(f"User logged out: {user_name}")
    flash('You have been logged out successfully.', 'success')
    return redirect(url_for('index'))


@app.route('/profile')
@user_login_required
def user_profile():
    """User profile page."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id = ?", (session['user_id'],))
        user = cursor.fetchone()
        
        # Get order statistics
        cursor.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) as delivered,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) as cancelled,
                SUM(total) as total_spent
            FROM orders WHERE user_id = ?
        """, (session['user_id'],))
        order_stats = cursor.fetchone()
        

        
        return render_template('auth/user_profile.html', 
                               user=dict(user) if user else None,
                               order_stats=dict(order_stats) if order_stats else {},
                               lang=lang, 
                               t=t)
                               
    except Exception as e:
        app.logger.error(f"Profile error: {str(e)}")
        flash('Error loading profile', 'error')
        return redirect(url_for('index'))


@app.route('/profile/update', methods=['POST'])
@user_login_required
def update_profile():
    """Update user profile."""
    full_name = request.form.get('full_name', '').strip()
    phone = request.form.get('phone', '').strip()
    address = request.form.get('address', '').strip()
    city = request.form.get('city', '').strip()
    
    if not full_name:
        flash('Full name is required', 'error')
        return redirect(url_for('user_profile'))
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE users SET 
                full_name = ?, phone = ?, address = ?, city = ?, 
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (full_name, phone, address, city, session['user_id']))
        conn.commit()

        
        # Update session
        session['user_name'] = full_name
        session['user_phone'] = phone
        
        flash('Profile updated successfully!', 'success')
        
    except Exception as e:
        app.logger.error(f"Profile update error: {str(e)}")
        flash('Error updating profile', 'error')
    
    return redirect(url_for('user_profile'))


@app.route('/profile/change-password', methods=['POST'])
@user_login_required
def change_password():
    """Change user password."""
    data = request.get_json()
    new_password = data.get('password', '')
    
    if not new_password or len(new_password) < 6:
        return jsonify({'success': False, 'error': 'Password must be at least 6 characters'}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        password_hash = generate_password_hash(new_password, method='pbkdf2:sha256')
        cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, session['user_id']))
        conn.commit()

        
        app.logger.info(f"Password changed for user {session['user_email']}")
        return jsonify({'success': True, 'message': 'Password changed successfully'})
        
    except Exception as e:
        app.logger.error(f"Password change error: {str(e)}")
        return jsonify({'success': False, 'error': 'Failed to change password'}), 500


@app.route('/delete-account', methods=['POST'])
@user_login_required
def delete_account():
    """Delete user account."""
    data = request.get_json()
    password = data.get('password', '')
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT password_hash FROM users WHERE id = ?", (session['user_id'],))
        user = cursor.fetchone()
        
        if not user or not check_password_hash(user[0], password):
            return jsonify({'success': False, 'error': 'Invalid password'}), 401
        
        # Delete user (cascade will delete orders and cart items)
        cursor.execute("DELETE FROM users WHERE id = ?", (session['user_id'],))
        conn.commit()

        
        # Clear session
        session.clear()
        
        app.logger.info(f"User account deleted: {session.get('user_email', 'Unknown')}")
        return jsonify({'success': True, 'message': 'Account deleted successfully'})
        
    except Exception as e:
        app.logger.error(f"Account deletion error: {str(e)}")
        return jsonify({'success': False, 'error': 'Failed to delete account'}), 500


@app.route('/orders')
@user_login_required
def user_orders():
    """User orders history page."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM orders 
            WHERE user_id = ? 
            ORDER BY id DESC
        """, (session['user_id'],))
        orders = cursor.fetchall()

        
        orders_list = [dict(order) for order in orders] if orders else []
        
        return render_template('auth/user_orders.html', 
                               orders=orders_list,
                               lang=lang, 
                               t=t)
                               
    except Exception as e:
        app.logger.error(f"Orders error: {str(e)}")
        flash('Error loading orders', 'error')
        return redirect(url_for('index'))


@app.route('/order/<int:order_id>')
@user_login_required
def order_detail(order_id):
    """Order detail page for users."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM orders WHERE id = ? AND user_id = ?", (order_id, session['user_id']))
        order = cursor.fetchone()
        
        if not order:
            flash('Order not found', 'error')
            return redirect(url_for('user_orders'))
        
        # Get order items
        cursor.execute("""
            SELECT oi.*, p.name, p.name_am, p.name_ar, p.thumbnail
            FROM order_items oi
            JOIN products p ON oi.product_id = p.id
            WHERE oi.order_id = ?
        """, (order_id,))
        items = cursor.fetchall()

        
        return render_template('auth/order_detail.html',
                               order=dict(order),
                               items=[dict(item) for item in items],
                               lang=lang,
                               t=t)
                               
    except Exception as e:
        app.logger.error(f"Order detail error: {str(e)}")
        flash('Error loading order details', 'error')
        return redirect(url_for('user_orders'))


@app.route('/order/<int:order_id>/invoice')
@user_login_required
def order_invoice(order_id):
    """Generate order invoice PDF."""
    # Placeholder for PDF generation
    flash('Invoice generation coming soon', 'info')
    return redirect(url_for('order_detail', order_id=order_id))


# ==================== 13. ADMIN AUTHENTICATION ROUTES ====================

@app.route('/login', methods=['GET', 'POST'])
def admin_login():
    """Admin login page."""
    if session.get('admin'):
        app.logger.info("Admin already logged in, redirecting to dashboard")
        return redirect(url_for('admin_dashboard'))
    
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        
        if not email or not password:
            flash('Please provide both email and password', 'error')
            return render_template('admin/login.html', lang=lang, t=t)
        
        # Get user from database
        user = User.get_by_email(email)
        
        if user and user['is_admin'] and check_password_hash(user['password_hash'], password):
            session['admin'] = True
            session['admin_username'] = user['username']
            session['admin_id'] = user['id']
            session.permanent = True
            app.logger.info(f"Admin logged in: {email}")
            
            flash('Welcome to Admin Panel!', 'success')
            
            next_page = request.args.get('next')
            if next_page and next_page.startswith('/'):
                return redirect(next_page)
            return redirect(url_for('admin_dashboard'))
        else:
            app.logger.warning(f"Failed admin login attempt for email: {email} from {request.remote_addr}")
            flash('Invalid email or password', 'error')
    
    return render_template('admin/login.html', lang=lang, t=t)


@app.route('/logout')
def admin_logout():
    """Admin logout."""
    if session.get('admin'):
        app.logger.info("Admin logged out")
        session.pop('admin', None)
        session.pop('admin_username', None)
        flash('You have been logged out successfully.', 'success')
    else:
        flash('You were not logged in.', 'info')
    
    return redirect(url_for('index'))


def is_admin():
    """Check if the current user is logged in as admin."""
    return session.get('admin', False)


def login_required_manual(f):
    """Decorator to require admin login for routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin'):
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('admin_login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function


# ==================== 14. FORGOT PASSWORD ROUTES ====================

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    """Forgot password page - request reset link."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        
        if not email:
            flash('Please enter your email address', 'error')
            return render_template('auth/forgot_password.html', lang=lang, t=t)
        
        try:
            conn = get_db()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, full_name FROM users WHERE email = ? AND is_active = 1", (email,))
            user = cursor.fetchone()
            
            if user:
                # Generate reset token
                reset_token = uuid.uuid4().hex
                expiry = datetime_.datetime.now().timestamp() + 3600  # 1 hour
                
                # Store token in session or database
                session['reset_token'] = reset_token
                session['reset_email'] = email
                session['reset_expiry'] = expiry
                
                # Send reset email
                send_password_reset_email(email, user[1], reset_token)
                flash('Password reset link sent to your email!', 'success')
            else:
                # Don't reveal if email exists for security
                flash('If an account exists with that email, you will receive a reset link.', 'info')
                
        except Exception as e:
            app.logger.error(f"Forgot password error: {str(e)}")
            flash('Error processing request. Please try again.', 'error')
    
    return render_template('auth/forgot_password.html', lang=lang, t=t)


def send_password_reset_email(email, name, token):
    """Send password reset email."""
    try:
        reset_link = url_for('reset_password', token=token, _external=True)
        app.logger.info(f"Password reset link for {email}: {reset_link}")
        # Implement actual email sending here
    except Exception as e:
        app.logger.error(f"Error sending reset email: {str(e)}")


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    """Reset password page."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    # Verify token
    stored_token = session.get('reset_token')
    stored_email = session.get('reset_email')
    stored_expiry = session.get('reset_expiry', 0)
    
    if not stored_token or stored_token != token or datetime_.datetime.now().timestamp() > stored_expiry:
        flash('Invalid or expired reset link. Please request a new one.', 'error')
        return redirect(url_for('forgot_password'))
    
    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        if not password or len(password) < 6:
            flash('Password must be at least 6 characters', 'error')
            return render_template('auth/reset_password.html', token=token, lang=lang, t=t)
        
        if password != confirm_password:
            flash('Passwords do not match', 'error')
            return render_template('auth/reset_password.html', token=token, lang=lang, t=t)
        
        try:
            conn = get_db()
            cursor = conn.cursor()
            password_hash = generate_password_hash(password, method='pbkdf2:sha256')
            cursor.execute("UPDATE users SET password_hash = ? WHERE email = ?", (password_hash, stored_email))
            conn.commit()

            
            # Clear reset session data
            session.pop('reset_token', None)
            session.pop('reset_email', None)
            session.pop('reset_expiry', None)
            
            flash('Password reset successful! Please login with your new password.', 'success')
            return redirect(url_for('user_login'))
            
        except Exception as e:
            app.logger.error(f"Reset password error: {str(e)}")
            flash('Error resetting password. Please try again.', 'error')
    
    return render_template('auth/reset_password.html', token=token, lang=lang, t=t)
# ==================== 15. CART ROUTES ====================

@app.route('/cart')
def view_cart():
    """Display the shopping cart page."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    cart_items = []
    subtotal = 0
    
    # Check if user is logged in
    if session.get('user_id'):
        try:
            conn = get_db()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT ci.*, p.name, p.name_am, p.name_ar, p.price, 
                       p.compare_price, p.thumbnail, p.stock_quantity
                FROM cart_items ci
                JOIN products p ON ci.product_id = p.id
                WHERE ci.user_id = ?
            """, (session['user_id'],))
            
            rows = cursor.fetchall()

            
            for row in rows:
                # Apply 10% discount for logged in users
                discounted_price = row['price'] * 0.9
                item_subtotal = discounted_price * row['quantity']
                subtotal += item_subtotal
                
                cart_items.append({
                    'id': row['id'],
                    'product_id': row['product_id'],
                    'name': row['name'],
                    'name_am': row['name_am'],
                    'name_ar': row['name_ar'],
                    'price': row['price'],
                    'discounted_price': round(discounted_price, 2),
                    'quantity': row['quantity'],
                    'thumbnail': row['thumbnail'],
                    'stock_quantity': row['stock_quantity'],
                    'subtotal': round(item_subtotal, 2)
                })
        except Exception as e:
            app.logger.error(f"Error loading cart from DB: {str(e)}")
    else:
        # Get cart from session
        cart = session.get('cart', {})
        if cart:
            try:
                conn = get_db()
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                placeholders = ','.join(['?'] * len(cart))
                cursor.execute(f"""
                    SELECT id, name, name_am, name_ar, price, compare_price, thumbnail, stock_quantity
                    FROM products WHERE id IN ({placeholders}) AND is_active = 1
                """, list(cart.keys()))
                
                products = cursor.fetchall()

                
                for p in products:
                    quantity = cart.get(str(p['id']), 0)
                    if quantity > 0:
                        item_subtotal = p['price'] * quantity
                        subtotal += item_subtotal
                        cart_items.append({
                            'product_id': p['id'],
                            'name': p['name'],
                            'name_am': p['name_am'],
                            'name_ar': p['name_ar'],
                            'price': p['price'],
                            'discounted_price': p['price'],
                            'quantity': quantity,
                            'thumbnail': p['thumbnail'],
                            'stock_quantity': p['stock_quantity'],
                            'subtotal': round(item_subtotal, 2)
                        })
            except Exception as e:
                app.logger.error(f"Error loading cart from session: {str(e)}")
    
    # Calculate discount (10% for logged in users)
    discount = 0
    if session.get('user_id'):
        discount = subtotal * 0.1
        subtotal_after_discount = subtotal - discount
    else:
        subtotal_after_discount = subtotal
    
    # Calculate shipping
    free_shipping_threshold = int(os.environ.get('FREE_SHIPPING_THRESHOLD', '5000'))
    if subtotal_after_discount >= free_shipping_threshold:
        shipping_cost = 0
        free_shipping = True
    else:
        shipping_cost = int(os.environ.get('SHIPPING_COST', '200'))
        free_shipping = False
    
    total = subtotal_after_discount + shipping_cost
    
    app.logger.info(f"Cart viewed - Items: {len(cart_items)}, Total: {total} ETB")
    
    return render_template('customer/cart.html',
                           cart_items=cart_items,
                           subtotal=round(subtotal, 2),
                           discount=round(discount, 2),
                           subtotal_after_discount=round(subtotal_after_discount, 2),
                           shipping_cost=shipping_cost,
                           total=round(total, 2),
                           free_shipping=free_shipping,
                           free_shipping_threshold=free_shipping_threshold,
                           lang=lang,
                           t=t)


@app.route('/cart/add', methods=['POST'])
def add_to_cart():
    """Add a product to the shopping cart."""
    try:
        product_id = request.form.get('product_id')
        quantity = int(request.form.get('quantity', 1))
        
        if not product_id:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'error': 'Product ID required'}), 400
            flash('Invalid product information.', 'error')
            return redirect(request.referrer or url_for('index'))
        
        # Check if product exists and has stock
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, name_am, price, stock_quantity FROM products WHERE id = ? AND is_active = 1", (product_id,))
        product = cursor.fetchone()
        
        if not product:

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'error': 'Product not found'}), 404
            flash('Product not found!', 'danger')
            return redirect(request.referrer or url_for('index'))
        
        if product[4] < quantity:

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'error': f'Only {product[4]} items available'}), 400
            flash(f'Sorry, only {product[4]} items available in stock!', 'warning')
            return redirect(request.referrer or url_for('index'))
        
        product_name = product[1] or product[2]
        product_price = product[3]
        
        if session.get('user_id'):
            # Add to database cart
            cursor.execute("""
                SELECT id, quantity FROM cart_items 
                WHERE user_id = ? AND product_id = ?
            """, (session['user_id'], product_id))
            
            existing = cursor.fetchone()
            
            if existing:
                new_quantity = existing[1] + quantity
                if product[4] >= new_quantity:
                    cursor.execute("""
                        UPDATE cart_items SET quantity = ? WHERE id = ?
                    """, (new_quantity, existing[0]))
                    message = 'Cart updated successfully!'
                else:

                    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                        return jsonify({'success': False, 'error': f'Only {product[4]} items available'}), 400
                    flash(f'Sorry, only {product[4]} items available in stock!', 'warning')
                    return redirect(request.referrer or url_for('index'))
            else:
                cursor.execute("""
                    INSERT INTO cart_items (user_id, product_id, quantity)
                    VALUES (?, ?, ?)
                """, (session['user_id'], product_id, quantity))
                message = 'Product added to cart!'
            
            conn.commit()

        else:
            # Add to session cart
            cart = session.get('cart', {})
            cart_key = str(product_id)
            current_quantity = cart.get(cart_key, 0)
            new_quantity = current_quantity + quantity
            
            if product[4] >= new_quantity:
                cart[cart_key] = new_quantity
                session['cart'] = cart
                session.modified = True
                message = 'Product added to cart!'
            else:
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({'success': False, 'error': f'Only {product[4]} items available'}), 400
                flash(f'Sorry, only {product[4]} items available in stock!', 'warning')
                return redirect(request.referrer or url_for('index'))
        
        # Get cart count
        cart_count = get_cart_count()
        
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({
                'success': True,
                'message': message,
                'cart_count': cart_count
            })
        
        flash(message, 'success')
        return redirect(url_for('view_cart'))
        
    except Exception as e:
        app.logger.error(f"Error adding to cart: {str(e)}")
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'error': 'Failed to add to cart'}), 500
        flash('Failed to add item to cart. Please try again.', 'error')
        return redirect(request.referrer or url_for('index'))


@app.route('/cart/update', methods=['POST'])
def update_cart():
    """Update item quantities in the shopping cart."""
    try:
        product_id = request.form.get('product_id')
        quantity = int(request.form.get('quantity', 1))
        
        if not product_id:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'error': 'Product ID required'}), 400
            flash('Invalid product information.', 'error')
            return redirect(url_for('view_cart'))
        
        if quantity < 1:
            return remove_from_cart(product_id)
        
        # Check stock
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT stock_quantity FROM products WHERE id = ?", (product_id,))
        product = cursor.fetchone()
        
        if product and quantity > product[0]:

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'error': f'Only {product[0]} items available'}), 400
            flash(f'Sorry, only {product[0]} items available in stock!', 'warning')
            return redirect(url_for('view_cart'))
        
        if session.get('user_id'):
            cursor.execute("""
                UPDATE cart_items SET quantity = ?
                WHERE user_id = ? AND product_id = ?
            """, (quantity, session['user_id'], product_id))
            conn.commit()
        else:
            cart = session.get('cart', {})
            cart[str(product_id)] = quantity
            session['cart'] = cart
            session.modified = True
        

        
        # Calculate new totals
        subtotal = get_cart_total()
        discount = subtotal * 0.1 if session.get('user_id') else 0
        subtotal_after_discount = subtotal - discount
        
        threshold = int(os.environ.get('FREE_SHIPPING_THRESHOLD', '5000'))
        shipping = 0 if subtotal_after_discount >= threshold else int(os.environ.get('SHIPPING_COST', '200'))
        total = subtotal_after_discount + shipping
        cart_count = get_cart_count()
        
        # Get item total for the updated item
        if session.get('user_id'):
            cursor.execute("""
                SELECT p.price * ci.quantity as item_total
                FROM cart_items ci
                JOIN products p ON ci.product_id = p.id
                WHERE ci.user_id = ? AND ci.product_id = ?
            """, (session['user_id'], product_id))
            item_total_row = cursor.fetchone()
            item_total = item_total_row['item_total'] if item_total_row else 0
        else:
            item_total = product[0] * quantity if product else 0
        
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({
                'success': True,
                'item_total': round(item_total, 2),
                'subtotal': round(subtotal, 2),
                'discount': round(discount, 2),
                'shipping': shipping,
                'total': round(total, 2),
                'cart_count': cart_count
            })
        
        flash('Cart updated successfully!', 'success')
        return redirect(url_for('view_cart'))
        
    except Exception as e:
        app.logger.error(f"Error updating cart: {str(e)}")
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'error': 'Failed to update cart'}), 500
        flash('Failed to update cart. Please try again.', 'error')
        return redirect(url_for('view_cart'))


@app.route('/cart/remove/<int:product_id>')
def remove_from_cart(product_id):
    """Remove an item from the shopping cart."""
    try:
        if session.get('user_id'):
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM cart_items 
                WHERE user_id = ? AND product_id = ?
            """, (session['user_id'], product_id))
            conn.commit()

        else:
            cart = session.get('cart', {})
            cart_key = str(product_id)
            if cart_key in cart:
                del cart[cart_key]
            session['cart'] = cart
            session.modified = True
        
        flash('Item removed from cart!', 'success')
        return redirect(url_for('view_cart'))
        
    except Exception as e:
        app.logger.error(f"Error removing from cart: {str(e)}")
        flash('Failed to remove item. Please try again.', 'error')
        return redirect(url_for('view_cart'))


@app.route('/cart/clear')
def clear_cart():
    """Clear all items from the shopping cart."""
    try:
        if session.get('user_id'):
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM cart_items WHERE user_id = ?", (session['user_id'],))
            conn.commit()

        else:
            session.pop('cart', None)
        
        flash('Cart has been cleared.', 'success')
        return redirect(url_for('view_cart'))
        
    except Exception as e:
        app.logger.error(f"Error clearing cart: {str(e)}")
        flash('Failed to clear cart. Please try again.', 'error')
        return redirect(url_for('view_cart'))


# ==================== 16. CHECKOUT ROUTES ====================

@app.route('/checkout', methods=['GET', 'POST'])
def checkout():
    """Checkout page - Collect customer information and process order."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    # Require login to checkout
    if not session.get('user_id'):
        flash('Please login to complete your purchase', 'warning')
        return redirect(url_for('user_login', next=request.url))
    
    # Get cart items
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ci.*, p.name, p.name_am, p.name_ar, p.price, p.compare_price, p.thumbnail
        FROM cart_items ci
        JOIN products p ON ci.product_id = p.id
        WHERE ci.user_id = ?
    """, (session['user_id'],))
    
    cart_items = cursor.fetchall()
    
    if not cart_items:
        flash('Your cart is empty', 'warning')
        return redirect(url_for('view_cart'))
    
    # Calculate totals — 10% discount applies to all registered users
    discount_message = 'የ 10% ቅናሽ ተጠቃሚ ነዎት! / You are a 10% discount user!'

    subtotal = 0
    items_list = []
    for item in cart_items:
        price = item['price'] if item['price'] else 0
        quantity = item['quantity'] if item['quantity'] else 1
        # Apply 10% discount to all registered users
        discounted_price = price * 0.9
        item_subtotal = discounted_price * quantity
        subtotal += item_subtotal
        items_list.append({
            'product_id': item['product_id'],
            'name': item['name'],
            'name_am': item['name_am'] if item['name_am'] else item['name'],
            'quantity': quantity,
            'price': discounted_price
        })

    discount = subtotal * 0.1
    subtotal_after_discount = subtotal - discount
    
    free_shipping_threshold = int(os.environ.get('FREE_SHIPPING_THRESHOLD', '5000'))
    if subtotal_after_discount >= free_shipping_threshold:
        shipping_cost = 0
    else:
        shipping_cost = int(os.environ.get('SHIPPING_COST', '200'))
    
    total = subtotal_after_discount + shipping_cost
    
    # Get user info for pre-filling
    cursor.execute("SELECT full_name, email, phone, address, city FROM users WHERE id = ?", (session['user_id'],))
    user = cursor.fetchone()
    
    if request.method == 'POST':
        try:
            shipping_address = request.form.get('shipping_address', '').strip()
            shipping_city = request.form.get('shipping_city', '').strip()
            shipping_phone = request.form.get('shipping_phone', '').strip()
            notes = request.form.get('notes', '').strip()
            payment_method = request.form.get('payment_method', 'cash')
            
            if not shipping_address or not shipping_phone:
                flash('Please fill in shipping address and phone number', 'error')
                return redirect(url_for('checkout'))
            
            # Create order with discount applied to all registered users
            order_id = create_order(
                user_id=session['user_id'],
                items=items_list,
                subtotal=subtotal,
                discount=discount,
                shipping_fee=shipping_cost,
                total=total,
                shipping_address=shipping_address,
                shipping_city=shipping_city,
                shipping_phone=shipping_phone,
                notes=notes,
                payment_method=payment_method
            )
            
            if order_id:
                # Clear cart
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM cart_items WHERE user_id = ?", (session['user_id'],))
                conn.commit()

                
                # Send WhatsApp notification
                send_order_whatsapp(session['user_name'], shipping_phone, items_list, total, order_id)
                
                flash('Order placed successfully!', 'success')
                return redirect(url_for('order_confirmation', order_id=order_id))
            else:
                flash('Failed to place order. Please try again.', 'error')
                
        except Exception as e:
            app.logger.error(f"Checkout error: {str(e)}")
            flash('An error occurred. Please try again.', 'error')
    
    return render_template('customer/checkout.html',
                           cart_items=cart_items,
                           subtotal=round(subtotal, 2),
                           discount=round(discount, 2),
                           subtotal_after_discount=round(subtotal_after_discount, 2),
                           shipping_cost=shipping_cost,
                           total=round(total, 2),
                           free_shipping_threshold=free_shipping_threshold,
                           android_discount=True,
                           discount_message=discount_message,
                           user=dict(user) if user else None,
                           lang=lang,
                           t=t)


def create_order(user_id, items, subtotal, discount, shipping_fee, total, 
                 shipping_address, shipping_city, shipping_phone, notes, payment_method):
    """Create a new order in the database."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Generate order number
        order_number = generate_order_number()
        
        cursor.execute("""
            INSERT INTO orders (
                order_number, user_id, status, payment_status, payment_method,
                subtotal, discount, shipping_fee, total,
                shipping_address, shipping_city, shipping_phone, notes,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            RETURNING id
        """, (
            order_number, user_id, 'pending', 'pending', payment_method,
            subtotal, discount, shipping_fee, total,
            shipping_address, shipping_city, shipping_phone, notes
        ))

        row = cursor.fetchone()
        order_id = row['id'] if row else None
        
        # Create order items
        for item in items:
            cursor.execute("""
                INSERT INTO order_items (order_id, product_id, quantity, price_at_time)
                VALUES (?, ?, ?, ?)
            """, (order_id, item['product_id'], item['quantity'], item['price']))
            
            # Update product stock
            cursor.execute("""
                UPDATE products SET 
                    stock_quantity = stock_quantity - ?,
                    sales_count = sales_count + ?
                WHERE id = ?
            """, (item['quantity'], item['quantity'], item['product_id']))
        
        conn.commit()

        
        app.logger.info(f"Order created: {order_number} by user {user_id}")
        return order_id
        
    except Exception as e:
        app.logger.error(f"Error creating order: {str(e)}")
        return None


def generate_order_number():
    """Generate a unique order number."""
    import random
    import string
    prefix = datetime_.datetime.now().strftime('%Y%m%d')
    random_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f'{prefix}-{random_str}'


def send_order_whatsapp(customer_name, customer_phone, items, total, order_id):
    """Send order notification via WhatsApp."""
    try:
        order_message = f"🛍️ *NEW ORDER - Ethiosadat Furniture*\n"
        order_message += "=" * 40 + "\n\n"
        order_message += f"📋 *Order #:* {order_id}\n"
        order_message += f"👤 *Customer:* {customer_name}\n"
        order_message += f"📞 *Phone:* {customer_phone}\n\n"
        order_message += "─" * 35 + "\n"
        order_message += "*ORDER ITEMS:*\n"
        order_message += "─" * 35 + "\n"
        
        for item in items:
            order_message += f"• {item['name_am']}\n"
            order_message += f"  {item['quantity']} x {item['price']} ETB\n\n"
        
        order_message += "─" * 35 + "\n"
        order_message += f"💰 *TOTAL:* {total} ETB\n"
        order_message += "=" * 40 + "\n"
        order_message += "🙏 Thank you for your order!"
        
        encoded = urllib.parse.quote(order_message)
        whatsapp_url = f"https://wa.me/{WHATSAPP_NUMBER}?text={encoded}"
        
        app.logger.info(f"WhatsApp order notification prepared")
        return whatsapp_url
        
    except Exception as e:
        app.logger.error(f"Error sending WhatsApp order: {str(e)}")
        return None


@app.route('/order-confirmation/<int:order_id>')
@user_login_required
def order_confirmation(order_id):
    """Order confirmation page after checkout."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM orders WHERE id = ? AND user_id = ?", (order_id, session['user_id']))
        order = cursor.fetchone()
        
        if not order:
            flash('Order not found!', 'danger')
            return redirect(url_for('index'))
        
        # Get order items
        cursor.execute("""
            SELECT oi.*, p.name, p.name_am, p.name_ar, p.thumbnail
            FROM order_items oi
            JOIN products p ON oi.product_id = p.id
            WHERE oi.order_id = ?
        """, (order_id,))
        items = cursor.fetchall()

        
        whatsapp_url = send_order_whatsapp(
            session['user_name'],
            order['shipping_phone'],
            [dict(item) for item in items],
            order['total'],
            order['order_number']
        )
        
        return render_template('auth/order_confirmation.html',
                               order=dict(order),
                               items=[dict(item) for item in items],
                               whatsapp_url=whatsapp_url,
                               lang=lang,
                               t=t)
                               
    except Exception as e:
        app.logger.error(f"Order confirmation error: {str(e)}")
        flash('Error loading order details', 'error')
        return redirect(url_for('index'))


# ==================== 17. CART HELPER FUNCTIONS ====================

def get_cart_count():
    """Get total number of items in cart."""
    count = 0
    
    if session.get('user_id'):
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT SUM(quantity) as total FROM cart_items WHERE user_id = ?", (session['user_id'],))
            result = cursor.fetchone()
            count = result[0] or 0
        except Exception as e:
            app.logger.error(f"Error getting cart count: {str(e)}")
    else:
        cart = session.get('cart', {})
        count = sum(cart.values())
    
    return count


def get_cart_total():
    """Get cart total amount."""
    total = 0
    
    if session.get('user_id'):
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT SUM(p.price * ci.quantity) as total
                FROM cart_items ci
                JOIN products p ON ci.product_id = p.id
                WHERE ci.user_id = ?
            """, (session['user_id'],))
            result = cursor.fetchone()
            total = result[0] or 0

        except Exception as e:
            app.logger.error(f"Error getting cart total: {str(e)}")
    else:
        cart = session.get('cart', {})
        if cart:
            try:
                conn = get_db()
                cursor = conn.cursor()
                placeholders = ','.join(['?'] * len(cart))
                cursor.execute(f"SELECT id, price FROM products WHERE id IN ({placeholders})", list(cart.keys()))
                products = cursor.fetchall()

                for p in products:
                    quantity = cart.get(str(p['id']), 0)
                    total += p['price'] * quantity
            except Exception as e:
                app.logger.error(f"Error getting cart total from session: {str(e)}")
    
    # Apply 10% discount only for logged-in Android App users
    if session.get('user_id') and is_android_app():
        total = total * 0.9
    
    return round(total, 2)
# ==================== 18. ADMIN ROUTES ====================

@app.route('/admin')
@admin_required
@limiter.exempt
def admin_dashboard():
    """Admin dashboard - Main admin panel with statistics."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
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
        
        cursor.execute("SELECT COUNT(*) FROM orders WHERE DATE(created_at) = DATE('now')")
        today_orders = cursor.fetchone()[0] or 0
        
        # Get recent products
        cursor.execute("""
            SELECT p.*, c.name as category_name 
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            ORDER BY p.id DESC LIMIT 5
        """)
        recent_products = cursor.fetchall()
        
        # Get recent orders
        cursor.execute("""
            SELECT * FROM orders ORDER BY id DESC LIMIT 5
        """)
        recent_orders = cursor.fetchall()
        
        # Get low stock products
        cursor.execute("""
            SELECT p.*, c.name as category_name 
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.stock_quantity <= p.low_stock_threshold AND p.stock_quantity > 0
            LIMIT 10
        """)
        low_stock_products = cursor.fetchall()

        # Total page views from settings table
        cursor.execute("SELECT value FROM settings WHERE key = 'page_views'")
        pv_row = cursor.fetchone()
        total_page_views = int(pv_row[0]) if pv_row and pv_row[0] else 0
        


        with _visitor_lock:
            live_visitors = len(_active_visitors)

        stats = {
            'products_count': products_count,
            'ads_count': ads_count,
            'total_orders': total_orders,
            'pending_orders': pending_orders,
            'customers_count': customers_count,
            'total_revenue': total_revenue,
            'today_orders': today_orders,
            'live_visitors': live_visitors,
            'total_page_views': total_page_views,
        }
        
        # Convert rows to dict for template
        recent_products_list = [dict(row) for row in recent_products] if recent_products else []
        recent_orders_list = [dict(row) for row in recent_orders] if recent_orders else []
        low_stock_list = [dict(row) for row in low_stock_products] if low_stock_products else []
        
        app.logger.info(f"Admin dashboard accessed - Products: {products_count}, Orders: {total_orders}")
        
        return render_template('admin/dashboard.html',
                               stats=stats,
                               recent_products=recent_products_list,
                               recent_orders=recent_orders_list,
                               low_stock_products=low_stock_list,
                               lang=lang,
                               t=t)
                               
    except Exception as e:
        app.logger.error(f"Error loading admin dashboard: {str(e)}")
        import traceback
        app.logger.error(traceback.format_exc())
        # ወደ ሎጊን ገጽ ከመላክ ይልቅ ስህተቱን በስክሪኑ ላይ እንዲያሳይህ እንዲህ አድርግ
        return f"Dashboard Error: {str(e)}", 500


# ==================== 19. ADMIN PRODUCT MANAGEMENT ====================

@app.route('/admin/products')
@admin_required
@limiter.exempt
def admin_products():
    """Admin product list - View all products with management options."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.*, c.name as category_name, c.name_am as category_name_am
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            ORDER BY p.id DESC
        """)
        products = cursor.fetchall()
        
        # Get categories for filter
        cursor.execute("SELECT * FROM categories WHERE is_active = 1 ORDER BY sort_order")
        categories = cursor.fetchall()
        

        
        products_list = [dict(p) for p in products] if products else []
        categories_list = [dict(cat) for cat in categories] if categories else []
        
        app.logger.info(f"Admin product list viewed - {len(products_list)} products")
        
        return render_template('admin/products/index.html',
                               products=products_list,
                               categories=categories_list,
                               lang=lang,
                               t=t)
                               
    except Exception as e:
        app.logger.error(f"Error loading admin products: {str(e)}")
        flash('Error loading products.', 'error')
        return redirect(url_for('admin_dashboard'))


@app.route('/admin/products/create', methods=['GET', 'POST'])
@admin_required
@limiter.exempt
def admin_product_create():
    """Create new product."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])

    # Get categories for dropdown (GET and POST both need them on redirect)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, name_am, name_ar FROM categories WHERE is_active = 1 ORDER BY sort_order")
    categories_list = [dict(cat) for cat in cursor.fetchall()]

    if request.method == 'POST':
        try:
            name_en = (request.form.get('name_en') or request.form.get('name', '')).strip()
            name_am = request.form.get('name_am', '').strip()
            name_ar = request.form.get('name_ar', '').strip()
            description_en = (request.form.get('description_en') or request.form.get('description', '')).strip()
            description_am = request.form.get('description_am', '').strip()
            description_ar = request.form.get('description_ar', '').strip()

            price = float(request.form.get('price', 0))
            compare_price_raw = request.form.get('compare_price', '').strip()
            compare_price = float(compare_price_raw) if compare_price_raw else None
            cost_raw = request.form.get('cost', '').strip()
            cost = float(cost_raw) if cost_raw else None

            stock_quantity = int(request.form.get('stock_quantity', 0))
            low_stock_threshold = int(request.form.get('low_stock_threshold', 5) or 5)
            category_id = int(request.form.get('category_id', 0))
            sku = request.form.get('sku', '').strip() or None
            barcode = request.form.get('barcode', '').strip() or None
            material = request.form.get('material', '').strip()
            color = request.form.get('color', '').strip()
            weight_raw = request.form.get('weight', '').strip()
            weight = float(weight_raw) if weight_raw else None
            dimensions = request.form.get('dimensions', '').strip() or None
            is_featured = 1 if request.form.get('is_featured') else 0
            is_new = 1 if request.form.get('is_new') else 0
            meta_title = request.form.get('meta_title', '').strip()
            meta_description = request.form.get('meta_description', '').strip()

            if not name_am or not name_en:
                flash('Please enter product name in both Amharic and English', 'error')
                return redirect(url_for('admin_product_create'))
            if price <= 0:
                flash('Please enter a valid price', 'error')
                return redirect(url_for('admin_product_create'))
            if category_id <= 0:
                flash('Please select a category', 'error')
                return redirect(url_for('admin_product_create'))

            # Handle image upload
            image_file = request.files.get('image')
            thumbnail_filename = ''
            if image_file and image_file.filename:
                if allowed_file(image_file.filename):
                    ext = image_file.filename.rsplit('.', 1)[1].lower()
                    fname = f"product_{uuid.uuid4().hex}_{datetime_.datetime.now().strftime('%Y%m%d%H%M%S')}.{ext}"
                    upload_path = os.path.join(app.config['UPLOAD_FOLDER'], 'products')
                    os.makedirs(upload_path, exist_ok=True)
                    image_file.save(os.path.join(upload_path, fname))
                    thumbnail_filename = f'uploads/products/{fname}'
                else:
                    flash('Invalid file type. Please upload an image.', 'error')
                    return redirect(url_for('admin_product_create'))

            product_id = Product.create({
                'name': name_en,
                'name_en': name_en,
                'name_am': name_am,
                'name_ar': name_ar,
                'description': description_en,
                'description_en': description_en,
                'description_am': description_am,
                'description_ar': description_ar,
                'price': price,
                'compare_price': compare_price,
                'cost': cost,
                'sku': sku,
                'barcode': barcode,
                'stock_quantity': stock_quantity,
                'low_stock_threshold': low_stock_threshold,
                'thumbnail': thumbnail_filename,
                'is_featured': is_featured,
                'is_new': is_new,
                'weight': weight,
                'dimensions': dimensions,
                'material': material,
                'color': color,
                'category_id': category_id,
                'meta_title': meta_title,
                'meta_description': meta_description,
            })

            if product_id is None:
                raise RuntimeError('Database insert failed — check server logs for details')

            app.logger.info(f"New product created: {name_am} (ID: {product_id})")
            flash(f'Product "{name_am}" created successfully!', 'success')
            return redirect(url_for('admin_products'))

        except Exception as e:
            app.logger.error(f"Error creating product: {str(e)}")
            import traceback
            app.logger.error(traceback.format_exc())
            flash(f'Error creating product: {str(e)}', 'error')
            return redirect(url_for('admin_product_create'))

    return render_template('admin/products/create.html',
                           categories=categories_list,
                           lang=lang,
                           t=t)


@app.route('/admin/products/edit/<int:pid>', methods=['GET', 'POST'])
@admin_required
@limiter.exempt
def admin_product_edit(pid):
    """Edit existing product."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])

    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM products WHERE id = ?", (pid,))
    product = cursor.fetchone()
    if not product:
        flash('Product not found!', 'danger')
        return redirect(url_for('admin_products'))

    cursor.execute("SELECT id, name, name_am, name_ar FROM categories WHERE is_active = 1 ORDER BY sort_order")
    categories_list = [dict(cat) for cat in cursor.fetchall()]
    product_dict = dict(product)

    if request.method == 'POST':
        try:
            name_en = (request.form.get('name_en') or request.form.get('name', '')).strip()
            name_am = request.form.get('name_am', '').strip()
            name_ar = request.form.get('name_ar', '').strip()
            description_en = (request.form.get('description_en') or request.form.get('description', '')).strip()
            description_am = request.form.get('description_am', '').strip()
            description_ar = request.form.get('description_ar', '').strip()

            price = float(request.form.get('price', 0))
            compare_price_raw = request.form.get('compare_price', '').strip()
            compare_price = float(compare_price_raw) if compare_price_raw else None
            cost_raw = request.form.get('cost', '').strip()
            cost = float(cost_raw) if cost_raw else None

            stock_quantity = int(request.form.get('stock_quantity', 0))
            low_stock_threshold = int(request.form.get('low_stock_threshold', 5) or 5)
            category_id = int(request.form.get('category_id', 0))
            sku = request.form.get('sku', '').strip() or None
            barcode = request.form.get('barcode', '').strip() or None
            material = request.form.get('material', '').strip()
            color = request.form.get('color', '').strip()
            weight_raw = request.form.get('weight', '').strip()
            weight = float(weight_raw) if weight_raw else None
            dimensions = request.form.get('dimensions', '').strip() or None
            is_featured = 1 if request.form.get('is_featured') else 0
            is_new = 1 if request.form.get('is_new') else 0
            meta_title = request.form.get('meta_title', '').strip()
            meta_description = request.form.get('meta_description', '').strip()

            # Handle image upload
            image_file = request.files.get('image')
            thumbnail_filename = product_dict.get('thumbnail', '')
            if image_file and image_file.filename:
                if allowed_file(image_file.filename):
                    if thumbnail_filename:
                        old_path = os.path.join('static', thumbnail_filename.lstrip('/'))
                        if os.path.exists(old_path):
                            try:
                                os.remove(old_path)
                            except Exception:
                                pass
                    ext = image_file.filename.rsplit('.', 1)[1].lower()
                    new_fname = f"product_{uuid.uuid4().hex}_{datetime_.datetime.now().strftime('%Y%m%d%H%M%S')}.{ext}"
                    upload_path = os.path.join(app.config['UPLOAD_FOLDER'], 'products')
                    os.makedirs(upload_path, exist_ok=True)
                    image_file.save(os.path.join(upload_path, new_fname))
                    thumbnail_filename = f'uploads/products/{new_fname}'
                else:
                    flash('Invalid file type. Please upload an image.', 'error')
                    return redirect(url_for('admin_product_edit', pid=pid))

            success = Product.update(pid, {
                'name': name_en,
                'name_en': name_en,
                'name_am': name_am,
                'name_ar': name_ar,
                'description': description_en,
                'description_en': description_en,
                'description_am': description_am,
                'description_ar': description_ar,
                'price': price,
                'compare_price': compare_price,
                'cost': cost,
                'sku': sku,
                'barcode': barcode,
                'stock_quantity': stock_quantity,
                'low_stock_threshold': low_stock_threshold,
                'thumbnail': thumbnail_filename,
                'is_featured': is_featured,
                'is_new': is_new,
                'weight': weight,
                'dimensions': dimensions,
                'material': material,
                'color': color,
                'category_id': category_id,
                'meta_title': meta_title,
                'meta_description': meta_description,
            })

            if not success:
                raise RuntimeError('Database update failed — check server logs for details')

            app.logger.info(f"Product updated: {name_am} (ID: {pid})")
            flash(f'Product "{name_am}" updated successfully!', 'success')
            return redirect(url_for('admin_products'))

        except Exception as e:
            app.logger.error(f"Error updating product: {str(e)}")
            import traceback
            app.logger.error(traceback.format_exc())
            flash(f'Error updating product: {str(e)}', 'error')
            return redirect(url_for('admin_product_edit', pid=pid))

    return render_template('admin/products/edit.html',
                           product=product_dict,
                           categories=categories_list,
                           lang=lang,
                           t=t)
@app.route('/admin/products/delete/<int:pid>')
@admin_required
def admin_product_delete(pid):
    """Delete product (soft delete)."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get product name for message
        cursor.execute("SELECT name_am, thumbnail FROM products WHERE id = ?", (pid,))
        product = cursor.fetchone()
        
        if product:
            # Delete thumbnail file from disk if it exists
            thumbnail = product[1] or ''
            if thumbnail:
                # Strip any accidental leading slash
                thumbnail_clean = thumbnail.lstrip('/')
                static_path = os.path.join('static', thumbnail_clean)
                if os.path.exists(static_path):
                    try:
                        os.remove(static_path)
                    except Exception:
                        pass

            # Hard delete from DB
            cursor.execute("DELETE FROM products WHERE id = ?", (pid,))
            conn.commit()
            app.logger.info(f"Product hard-deleted: {product[0]} (ID: {pid})")
            flash(f'Product "{product[0]}" deleted successfully!', 'success')
        else:
            flash('Product not found.', 'error')
        

        return redirect(url_for('admin_products'))
        
    except Exception as e:
        app.logger.error(f"Error deleting product {pid}: {str(e)}")
        flash('Error deleting product.', 'error')
        return redirect(url_for('admin_products'))


@app.route('/admin/products/toggle-featured/<int:pid>', methods=['POST'])
@admin_required
def admin_product_toggle_featured(pid):
    """Toggle product featured status."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT is_featured FROM products WHERE id = ?", (pid,))
        row = cursor.fetchone()
        if not row:

            return jsonify({'success': False, 'error': 'Product not found'}), 404
        new_state = 0 if row[0] else 1
        cursor.execute("UPDATE products SET is_featured = ? WHERE id = ?", (new_state, pid))
        conn.commit()


        return jsonify({'success': True, 'is_featured': bool(new_state)})
        
    except Exception as e:
        app.logger.error(f"Error toggling featured: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/products/update-stock/<int:pid>', methods=['POST'])
@admin_required
def admin_product_update_stock(pid):
    """Update product stock quantity."""
    try:
        data = request.get_json()
        stock_quantity = int(data.get('stock_quantity', 0))
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE products SET stock_quantity = ? WHERE id = ?", (stock_quantity, pid))
        conn.commit()

        
        return jsonify({'success': True, 'stock_quantity': stock_quantity})
        
    except Exception as e:
        app.logger.error(f"Error updating stock: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/products/bulk-delete', methods=['POST'])
@admin_required
def admin_bulk_delete_products():
    """Delete multiple products at once."""
    try:
        data = request.get_json()
        ids = data.get('ids', [])
        
        if not ids:
            return jsonify({'success': False, 'error': 'No products selected'}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        placeholders = ','.join(['?'] * len(ids))

        # Fetch thumbnails to clean up files
        cursor.execute(f"SELECT id, name_am, thumbnail FROM products WHERE id IN ({placeholders})", ids)
        products = cursor.fetchall()

        # Delete image files from disk
        for prod in products:
            thumb = prod[2] or ''
            if thumb:
                static_path = os.path.join('static', thumb.lstrip('/'))
                if os.path.exists(static_path):
                    try:
                        os.remove(static_path)
                    except Exception:
                        pass

        # Hard delete from DB
        cursor.execute(f"DELETE FROM products WHERE id IN ({placeholders})", ids)
        conn.commit()


        app.logger.info(f"Bulk hard-deleted {len(ids)} products")

        return jsonify({'success': True, 'deleted': len(ids)})
        
    except Exception as e:
        app.logger.error(f"Error bulk deleting products: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/products/export')
@admin_required
def admin_export_products():
    """Export products to CSV."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.id, p.name, p.name_am, p.name_ar, p.price, p.compare_price,
                   p.stock_quantity, c.name as category_name, p.is_featured, p.created_at
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.is_active = 1
            ORDER BY p.id DESC
        """)
        products = cursor.fetchall()

        
        import csv
        from io import StringIO
        
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(['ID', 'Name (EN)', 'Name (AM)', 'Name (AR)', 'Price', 'Compare Price', 
                        'Stock', 'Category', 'Featured', 'Created Date'])
        
        for p in products:
            writer.writerow([
                p['id'], p['name'] or '', p['name_am'] or '', p['name_ar'] or '',
                p['price'], p['compare_price'] or '',
                p['stock_quantity'], p['category_name'] or '',
                'Yes' if p['is_featured'] else 'No', p['created_at']
            ])
        
        response = make_response(output.getvalue())
        response.headers['Content-Type'] = 'text/csv'
        response.headers['Content-Disposition'] = f'attachment; filename=products_export_{datetime_.datetime.now().strftime("%Y%m%d")}.csv'
        
        return response
        
    except Exception as e:
        app.logger.error(f"Export error: {str(e)}")
        flash('Error exporting products', 'error')
        return redirect(url_for('admin_products'))
# ==================== 20. ADMIN ADVERTISEMENT MANAGEMENT ====================

@app.route('/admin/ads')
@admin_required
@limiter.exempt
def admin_ads():
    """Admin advertisement list."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM advertisements 
            ORDER BY sort_order ASC, id DESC
        """)
        ads = cursor.fetchall()

        
        ads_list = []
        if ads:
            for ad in ads:
                ad_dict = dict(ad)
                # ቀኑን ወደ ተነባቢ ጽሁፍ (YYYY-MM-DD) የመቀየሪያ ዘዴ
                if ad_dict.get('created_at') and isinstance(ad_dict['created_at'], str):
                    ad_dict['formatted_date'] = ad_dict['created_at'][:10]
                elif ad_dict.get('created_at'):
                    ad_dict['formatted_date'] = ad_dict['created_at'].strftime('%Y-%m-%d')
                else:
                    ad_dict['formatted_date'] = "N/A"
                ads_list.append(ad_dict)
        
        # Get statistics
        active_ads = sum(1 for ad in ads_list if ad.get('is_active', 0) == 1)
        ads_with_media = sum(1 for ad in ads_list if ad.get('image') and ad['image'] != '')
        
        app.logger.info(f"Admin ads list viewed - {len(ads_list)} ads")
        
        return render_template('admin/ads/index.html',
                               ads=ads_list,
                               total_ads=len(ads_list),
                               active_ads=active_ads,
                               ads_with_media=ads_with_media,
                               lang=lang,
                               t=t)
                               
    except Exception as e:
        app.logger.error(f"Error loading admin ads: {str(e)}")
        flash('Error loading advertisements.', 'error')
        return redirect(url_for('admin_dashboard'))


@app.route('/admin/ads/create', methods=['GET', 'POST'])
@admin_required
@limiter.exempt
def admin_ad_create():
    """Create new advertisement."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    if request.method == 'POST':
        try:
            title = request.form.get('title', '').strip()
            title_am = request.form.get('title_am', '').strip()
            title_ar = request.form.get('title_ar', '').strip()
            # Support both 'ad_text' (form field name) and 'description' (legacy)
            description = request.form.get('ad_text', request.form.get('description', '')).strip()
            description_am = request.form.get('description_am', '').strip()
            description_ar = request.form.get('description_ar', '').strip()
            link = request.form.get('link', '').strip()
            sort_order = int(request.form.get('sort_order', 0))
            is_active = 1 if request.form.get('is_active') else 0
            
            start_date = request.form.get('start_date') or None
            end_date = request.form.get('end_date') or None
            
            # Validate required fields
            if not description and not title:
                flash('Please enter advertisement text or title', 'error')
                return redirect(url_for('admin_ad_create'))
            
            # Handle media upload
            media_file = request.files.get('media')
            media_url = request.form.get('media_url', '').strip()
            image_filename = ''
            
            if media_file and media_file.filename:
                if allowed_file(media_file.filename):
                    ext = media_file.filename.rsplit('.', 1)[1].lower()
                    image_filename = f"ad_{uuid.uuid4().hex}_{datetime_.datetime.now().strftime('%Y%m%d%H%M%S')}.{ext}"
                    upload_path = os.path.join(app.config['UPLOAD_FOLDER'], 'ads')
                    os.makedirs(upload_path, exist_ok=True)
                    media_file.save(os.path.join(upload_path, image_filename))
                    image_filename = f'uploads/ads/{image_filename}'
                else:
                    flash('Invalid file type. Please upload an image or video.', 'error')
                    return redirect(url_for('admin_ad_create'))
            elif media_url:
                image_filename = media_url
            
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO advertisements (
                    title, title_am, title_ar,
                    description, description_am, description_ar,
                    image, link, sort_order, is_active,
                    start_date, end_date, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                RETURNING id
            """, (title, title_am, title_ar, description, description_am, description_ar,
                  image_filename, link, sort_order, is_active, start_date, end_date))

            row = cursor.fetchone()
            conn.commit()
            ad_id = row['id'] if row else None

            
            app.logger.info(f"New advertisement created: {title or description[:50]} (ID: {ad_id})")
            flash('Advertisement created successfully!', 'success')
            return redirect(url_for('admin_ads'))
            
        except Exception as e:
            app.logger.error(f"Error creating advertisement: {str(e)}")
            import traceback
            app.logger.error(traceback.format_exc())
            flash('Error creating advertisement. Please try again.', 'error')
    
    return render_template('admin/ads/create.html', lang=lang, t=t)


@app.route('/admin/ads/edit/<int:aid>', methods=['GET', 'POST'])
@admin_required
@limiter.exempt
def admin_ad_edit(aid):
    """Edit existing advertisement."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])

    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM advertisements WHERE id = ?", (aid,))
    ad = cursor.fetchone()

    if not ad:
        flash('Advertisement not found!', 'danger')
        return redirect(url_for('admin_ads'))

    ad_dict = dict(ad)

    if request.method == 'POST':
        try:
            title = request.form.get('title', '').strip()
            title_am = request.form.get('title_am', '').strip()
            title_ar = request.form.get('title_ar', '').strip()
            # Support both 'ad_text' (form field name) and 'description' (legacy)
            description = request.form.get('ad_text', request.form.get('description', '')).strip()
            description_am = request.form.get('description_am', '').strip()
            description_ar = request.form.get('description_ar', '').strip()
            link = request.form.get('link', '').strip()
            sort_order = int(request.form.get('sort_order', 0))
            is_active = 1 if request.form.get('is_active') else 0

            start_date = request.form.get('start_date') or None
            end_date = request.form.get('end_date') or None

            # Handle media upload
            media_file = request.files.get('media')
            media_url = request.form.get('media_url', '').strip()
            image_filename = ad_dict.get('image', '')

            if media_file and media_file.filename:
                if allowed_file(media_file.filename):
                    # Delete old image if exists and is local file
                    if image_filename and image_filename.startswith('uploads/') and os.path.exists(image_filename):
                        try:
                            os.remove(image_filename)
                        except Exception:
                            pass

                    ext = media_file.filename.rsplit('.', 1)[1].lower()
                    image_filename = f"ad_{uuid.uuid4().hex}_{datetime_.datetime.now().strftime('%Y%m%d%H%M%S')}.{ext}"
                    upload_path = os.path.join(app.config['UPLOAD_FOLDER'], 'ads')
                    os.makedirs(upload_path, exist_ok=True)
                    media_file.save(os.path.join(upload_path, image_filename))
                    image_filename = f'uploads/ads/{image_filename}'
                else:
                    flash('Invalid file type. Please upload an image or video.', 'error')
                    return redirect(url_for('admin_ad_edit', aid=aid))
            elif media_url:
                image_filename = media_url

            cursor = conn.cursor()
            # ማስተካከያ፡ 'updated_at=CURRENT_TIMESTAMP' የሚለው ተወግዷል
            cursor.execute("""
                UPDATE advertisements SET
                    title=?, title_am=?, title_ar=?,
                    description=?, description_am=?, description_ar=?,
                    image=?, link=?, sort_order=?, is_active=?,
                    start_date=?, end_date=?
                WHERE id=?
            """, (
                title, title_am, title_ar, description, description_am, description_ar,
                image_filename, link, sort_order, is_active, start_date, end_date, aid
            ))

            conn.commit()

            app.logger.info(f"Advertisement updated: ID {aid}")
            flash('Advertisement updated successfully!', 'success')
            return redirect(url_for('admin_ads'))

        except Exception as e:
            app.logger.error(f"Error updating advertisement: {str(e)}")
            flash('Error updating advertisement. Please try again.', 'error')

    return render_template('admin/ads/edit.html', ad=ad_dict, lang=lang, t=t)
@app.route('/admin/ads/toggle/<int:aid>', methods=['POST'])
@admin_required
def admin_ad_toggle(aid):
    """Toggle advertisement active status."""
    try:
        data = request.get_json() or {}
        # Support both 'status' (JS sends 'active'/'inactive') and 'is_active' (boolean)
        if 'status' in data:
            is_active = data['status'] == 'active'
        else:
            is_active = bool(data.get('is_active', False))
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE advertisements SET is_active = ? WHERE id = ?", (1 if is_active else 0, aid))
        conn.commit()

        
        app.logger.info(f"Ad {aid} status toggled to: {'active' if is_active else 'inactive'}")
        return jsonify({'success': True, 'is_active': is_active})
        
    except Exception as e:
        app.logger.error(f"Error toggling ad status: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/ads/delete/<int:aid>', methods=['DELETE'])
@admin_required
def admin_ad_delete(aid):
    """Delete advertisement."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get image path to delete
        cursor.execute("SELECT image FROM advertisements WHERE id = ?", (aid,))
        ad = cursor.fetchone()
        
        if ad and ad[0] and ad[0].startswith('uploads/') and os.path.exists(ad[0]):
            try:
                os.remove(ad[0])
            except:
                pass
        
        cursor.execute("DELETE FROM advertisements WHERE id = ?", (aid,))
        conn.commit()

        
        app.logger.info(f"Advertisement deleted: ID {aid}")
        return jsonify({'success': True, 'message': 'Advertisement deleted successfully'})
        
    except Exception as e:
        app.logger.error(f"Error deleting advertisement: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/ads/reorder', methods=['POST'])
@admin_required
def admin_ad_reorder():
    """Reorder advertisements."""
    try:
        data = request.get_json()
        src_id = data.get('src_id')
        dest_id = data.get('dest_id')
        
        if not src_id or not dest_id:
            return jsonify({'success': False, 'error': 'Invalid request'}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Get current sort orders
        cursor.execute("SELECT sort_order FROM advertisements WHERE id = ?", (src_id,))
        src_order = cursor.fetchone()
        cursor.execute("SELECT sort_order FROM advertisements WHERE id = ?", (dest_id,))
        dest_order = cursor.fetchone()
        
        if src_order and dest_order:
            cursor.execute("UPDATE advertisements SET sort_order = ? WHERE id = ?", (dest_order[0], src_id))
            cursor.execute("UPDATE advertisements SET sort_order = ? WHERE id = ?", (src_order[0], dest_id))
            conn.commit()
        
        
        return jsonify({'success': True})
        
    except Exception as e:
        app.logger.error(f"Error reordering ads: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== 21. ADMIN ORDER MANAGEMENT ====================

@app.route('/admin/orders')
@admin_required
@limiter.exempt
def admin_orders():
    """Admin orders list."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get filter parameters
        status = request.args.get('status', 'all')
        search = request.args.get('search', '').strip()
        
        query = """
            SELECT o.*, u.full_name as customer_name
            FROM orders o
            LEFT JOIN users u ON o.user_id = u.id
            WHERE 1=1
        """
        params = []
        
        if status != 'all':
            query += " AND o.status = ?"
            params.append(status)
        
        if search:
            query += " AND (o.order_number LIKE ? OR u.full_name LIKE ? OR o.shipping_phone LIKE ?)"
            search_term = f'%{search}%'
            params.extend([search_term, search_term, search_term])
        
        query += " ORDER BY o.id DESC"
        
        cursor.execute(query, params)
        orders = cursor.fetchall()
        
        # Get order counts by status for dashboard
        cursor.execute("""
            SELECT status, COUNT(*) as count 
            FROM orders 
            GROUP BY status
        """)
        status_counts = {row['status']: row['count'] for row in cursor.fetchall()}
        

        
        orders_list = [dict(order) for order in orders] if orders else []
        
        app.logger.info(f"Admin orders list viewed - {len(orders_list)} orders")
        
        return render_template('admin/orders/index.html',
                               orders=orders_list,
                               status_counts=status_counts,
                               current_status=status,
                               search=search,
                               lang=lang,
                               t=t)
                               
    except Exception as e:
        app.logger.error(f"Error loading admin orders: {str(e)}")
        flash('Error loading orders.', 'error')
        return redirect(url_for('admin_dashboard'))


@app.route('/admin/orders/<int:oid>')
@admin_required
@limiter.exempt
def admin_order_detail(oid):
    """Admin order detail page."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT o.*, u.full_name, u.email, u.phone
            FROM orders o
            LEFT JOIN users u ON o.user_id = u.id
            WHERE o.id = ?
        """, (oid,))
        order = cursor.fetchone()
        
        if not order:
            flash('Order not found!', 'danger')
            return redirect(url_for('admin_orders'))
        
        # Get order items
        cursor.execute("""
            SELECT oi.*, p.name, p.name_am, p.name_ar, p.thumbnail
            FROM order_items oi
            JOIN products p ON oi.product_id = p.id
            WHERE oi.order_id = ?
        """, (oid,))
        items = cursor.fetchall()
        

        
        order_dict = dict(order)
        items_list = [dict(item) for item in items] if items else []
        
        return render_template('admin/orders/detail.html',
                               order=order_dict,
                               order_items=items_list,
                               lang=lang,
                               t=t)
                               
    except Exception as e:
        app.logger.error(f"Error loading order {oid}: {str(e)}")
        flash('Error loading order details.', 'error')
        return redirect(url_for('admin_orders'))


@app.route('/admin/orders/update/<int:oid>', methods=['POST'])
@admin_required
def admin_order_update_status(oid):
    """Update order status."""
    try:
        status = request.form.get('status', 'pending')
        
        valid_statuses = ['pending', 'confirmed', 'processing', 'shipped', 'delivered', 'cancelled']
        if status not in valid_statuses:
            return jsonify({'success': False, 'error': 'Invalid status'}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE orders SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status, oid))
        conn.commit()

        
        app.logger.info(f"Order {oid} status updated to: {status}")
        
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True, 'status': status})
        
        flash(f'Order status updated to {status}!', 'success')
        return redirect(url_for('admin_order_detail', oid=oid))
        
    except Exception as e:
        app.logger.error(f"Error updating order {oid}: {str(e)}")
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'error': str(e)}), 500
        flash('Error updating order status.', 'error')
        return redirect(url_for('admin_order_detail', oid=oid))


@app.route('/admin/orders/delete/<int:oid>')
@admin_required
def admin_delete_order(oid):
    """Delete order."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Delete order items first (cascade)
        cursor.execute("DELETE FROM order_items WHERE order_id = ?", (oid,))
        cursor.execute("DELETE FROM orders WHERE id = ?", (oid,))
        conn.commit()

        
        app.logger.info(f"Order {oid} deleted")
        flash('Order deleted successfully!', 'success')
        return redirect(url_for('admin_orders'))
        
    except Exception as e:
        app.logger.error(f"Error deleting order {oid}: {str(e)}")
        flash('Error deleting order.', 'error')
        return redirect(url_for('admin_orders'))


@app.route('/admin/orders/export/<int:oid>')
@admin_required
def admin_export_order(oid):
    """Export single order as JSON."""
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT o.*, u.full_name, u.email, u.phone
            FROM orders o
            LEFT JOIN users u ON o.user_id = u.id
            WHERE o.id = ?
        """, (oid,))
        order = cursor.fetchone()
        
        if not order:
            flash('Order not found!', 'danger')
            return redirect(url_for('admin_orders'))
        
        cursor.execute("""
            SELECT oi.*, p.name, p.name_am, p.name_ar
            FROM order_items oi
            JOIN products p ON oi.product_id = p.id
            WHERE oi.order_id = ?
        """, (oid,))
        items = cursor.fetchall()
        

        
        export_data = {
            'order': dict(order),
            'items': [dict(item) for item in items]
        }
        
        response = make_response(json.dumps(export_data, indent=2, default=str))
        response.headers['Content-Type'] = 'application/json'
        response.headers['Content-Disposition'] = f'attachment; filename=order_{oid}_{datetime_.datetime.now().strftime("%Y%m%d")}.json'
        
        return response
        
    except Exception as e:
        app.logger.error(f"Error exporting order {oid}: {str(e)}")
        flash('Error exporting order.', 'error')
        return redirect(url_for('admin_orders'))


@app.route('/admin/orders/invoice/<int:oid>')
@admin_required
def admin_order_invoice(oid):
    """Generate invoice PDF for order."""
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM orders WHERE id = ?", (oid,))
        order = cursor.fetchone()
        
        if not order:
            flash('Order not found!', 'danger')
            return redirect(url_for('admin_orders'))
        
        cursor.execute("""
            SELECT oi.*, p.name, p.name_am, p.name_ar
            FROM order_items oi
            JOIN products p ON oi.product_id = p.id
            WHERE oi.order_id = ?
        """, (oid,))
        items = cursor.fetchall()
        

        
        # Generate HTML invoice
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>Invoice #{order['order_number']}</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; }}
                .header {{ text-align: center; margin-bottom: 30px; }}
                .invoice-title {{ font-size: 28px; color: #1a73e8; }}
                .order-info {{ margin-bottom: 20px; }}
                table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
                th, td {{ border: 1px solid #ddd; padding: 10px; text-align: left; }}
                th {{ background: #1a73e8; color: white; }}
                .total {{ font-size: 18px; font-weight: bold; text-align: right; }}
                .footer {{ margin-top: 40px; text-align: center; font-size: 12px; color: #666; }}
            </style>
        </head>
        <body>
            <div class="header">
                <h1 class="invoice-title">🪑 Ethiosadat Furniture</h1>
                <h2>INVOICE</h2>
            </div>
            <div class="order-info">
                <p><strong>Order #:</strong> {order['order_number']}</p>
                <p><strong>Date:</strong> {order['created_at']}</p>
                <p><strong>Status:</strong> {order['status']}</p>
            </div>
            <table>
                <thead>
                    <tr><th>Product</th><th>Quantity</th><th>Price</th><th>Total</th></tr>
                </thead>
                <tbody>
        """
        
        for item in items:
            price = item['price_at_time']
            total = price * item['quantity']
            html += f"<tr><td>{item['name_am']}</td><td>{item['quantity']}</td><td>{price:.2f} ETB</td><td>{total:.2f} ETB</td></tr>"
        
        html += f"""
                </tbody>
            </table>
            <div class="total">
                <p>Subtotal: {order['subtotal']:.2f} ETB</p>
                <p>Discount: {order['discount']:.2f} ETB</p>
                <p>Shipping: {order['shipping_fee']:.2f} ETB</p>
                <p><strong>Total: {order['total']:.2f} ETB</strong></p>
            </div>
            <div class="footer">
                <p>Thank you for shopping with Ethiosadat Furniture!</p>
                <p>Addis Ababa, Ethiopia | +251 90 602 0606 | info@ethiosadat.com</p>
            </div>
        </body>
        </html>
        """
        
        return html
        
    except Exception as e:
        app.logger.error(f"Error generating invoice: {str(e)}")
        flash('Error generating invoice.', 'error')
        return redirect(url_for('admin_order_detail', oid=oid))
# ==================== 22. ADMIN USERS ====================

@app.route('/admin/users')
@admin_required
def admin_users():
    """Admin users management page."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, username, email, full_name, phone, is_admin, is_active, created_at, last_login
            FROM users
            ORDER BY created_at DESC
        """)
        users = [dict(u) for u in cursor.fetchall()]
        return render_template('admin/users/index.html', users=users, lang=lang, t=t)
    except Exception as e:
        app.logger.error(f"Admin users error: {str(e)}")
        flash('Error loading users.', 'error')
        return redirect(url_for('admin_dashboard'))


@app.route('/admin/users/toggle/<int:uid>', methods=['POST'])
@admin_required
def admin_toggle_user(uid):
    """Toggle user active status."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT is_active, is_admin FROM users WHERE id = ?", (uid,))
        user = cursor.fetchone()
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        if user['is_admin']:
            return jsonify({'success': False, 'error': 'Cannot deactivate admin accounts'}), 403
        new_status = 0 if user['is_active'] else 1
        cursor.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_status, uid))
        conn.commit()
        return jsonify({'success': True, 'is_active': new_status})
    except Exception as e:
        app.logger.error(f"Toggle user error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/users/delete/<int:uid>', methods=['POST'])
@admin_required
def admin_delete_user(uid):
    """Delete a user account."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT is_admin FROM users WHERE id = ?", (uid,))
        user = cursor.fetchone()
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        if user['is_admin']:
            return jsonify({'success': False, 'error': 'Cannot delete admin accounts'}), 403
        cursor.execute("DELETE FROM cart_items WHERE user_id = ?", (uid,))
        cursor.execute("DELETE FROM users WHERE id = ?", (uid,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        app.logger.error(f"Delete user error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== 23. ADMIN REPORTS ====================

@app.route('/admin/reports')
@admin_required
@limiter.exempt
def admin_reports():
    """Reports dashboard with analytics."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get total counts
        cursor.execute("SELECT COUNT(*) FROM products WHERE is_active = 1")
        total_products = cursor.fetchone()[0] or 0
        
        cursor.execute("SELECT COUNT(*) FROM orders")
        total_orders = cursor.fetchone()[0] or 0
        
        cursor.execute("SELECT COUNT(*) FROM users WHERE is_admin = 0")
        total_customers = cursor.fetchone()[0] or 0
        
        # Get revenue stats
        cursor.execute("SELECT SUM(total) FROM orders WHERE status = 'delivered'")
        total_revenue = cursor.fetchone()[0] or 0
        
        cursor.execute("SELECT COUNT(*) FROM orders WHERE status = 'pending'")
        pending_orders = cursor.fetchone()[0] or 0
        
        cursor.execute("SELECT COUNT(*) FROM orders WHERE status = 'delivered'")
        completed_orders = cursor.fetchone()[0] or 0
        
        # Get category distribution
        cursor.execute("""
            SELECT c.name_am, c.name, COUNT(p.id) as product_count
            FROM categories c
            LEFT JOIN products p ON p.category_id = c.id AND p.is_active = 1
            WHERE c.is_active = 1
            GROUP BY c.id
            ORDER BY product_count DESC
        """)
        categories = cursor.fetchall()
        
        # Get monthly sales (last 12 months)
        cursor.execute("""
            SELECT TO_CHAR(created_at, 'YYYY-MM') as month,
                   COUNT(*) as order_count,
                   SUM(total) as revenue
            FROM orders
            WHERE status != 'cancelled'
            AND created_at >= NOW() - INTERVAL '12 months'
            GROUP BY TO_CHAR(created_at, 'YYYY-MM')
            ORDER BY month DESC
        """)
        monthly_sales = cursor.fetchall()
        
        # Get top selling products
        cursor.execute("""
            SELECT p.id, p.name_am, p.name, SUM(oi.quantity) as total_sold,
                   SUM(oi.quantity * oi.price_at_time) as revenue
            FROM order_items oi
            JOIN products p ON oi.product_id = p.id
            GROUP BY p.id
            ORDER BY total_sold DESC
            LIMIT 10
        """)
        top_products = cursor.fetchall()
        
        # Get low stock products
        cursor.execute("""
            SELECT p.id, p.name_am, p.name, p.stock_quantity, p.low_stock_threshold
            FROM products p
            WHERE p.stock_quantity <= p.low_stock_threshold AND p.stock_quantity > 0
            ORDER BY p.stock_quantity ASC
            LIMIT 10
        """)
        low_stock = cursor.fetchall()
        

        
        # Convert to dict
        categories_list = [dict(cat) for cat in categories] if categories else []
        monthly_sales_list = [dict(sale) for sale in monthly_sales] if monthly_sales else []
        top_products_list = [dict(p) for p in top_products] if top_products else []
        low_stock_list = [dict(p) for p in low_stock] if low_stock else []
        
        stats = {
            'total_products': total_products,
            'total_orders': total_orders,
            'total_customers': total_customers,
            'total_revenue': total_revenue,
            'pending_orders': pending_orders,
            'completed_orders': completed_orders
        }
        
        app.logger.info("Admin reports viewed")
        
        return render_template('admin/reports/index.html',
                               stats=stats,
                               categories=categories_list,
                               monthly_sales=monthly_sales_list,
                               top_products=top_products_list,
                               low_stock=low_stock_list,
                               total_products=total_products,
                               total_orders=total_orders,
                               total_revenue=total_revenue,
                               total_customers=total_customers,
                               pending_orders=pending_orders,
                               completed_orders=completed_orders,
                               lang=lang,
                               t=t)
                               
    except Exception as e:
        app.logger.error(f"Error loading reports: {str(e)}")
        import traceback
        app.logger.error(traceback.format_exc())
        flash('Error loading reports.', 'error')
        return redirect(url_for('admin_dashboard'))


@app.route('/admin/reports/sales')
@admin_required
@limiter.exempt
def admin_reports_sales():
    """Detailed sales report with date filtering."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        query = """
            SELECT o.*, u.full_name
            FROM orders o
            LEFT JOIN users u ON o.user_id = u.id
            WHERE o.status != 'cancelled'
        """
        params = []
        
        if start_date:
            query += " AND DATE(o.created_at) >= ?"
            params.append(start_date)
        if end_date:
            query += " AND DATE(o.created_at) <= ?"
            params.append(end_date)
        
        query += " ORDER BY o.created_at DESC"
        
        cursor.execute(query, params)
        orders = cursor.fetchall()
        
        # Calculate totals
        total_revenue = sum(o['total'] for o in orders) if orders else 0
        total_orders = len(orders)
        avg_order_value = total_revenue / total_orders if total_orders > 0 else 0
        
        # Get daily sales for chart
        cursor.execute("""
            SELECT DATE(created_at) as date, 
                   COUNT(*) as order_count, 
                   SUM(total) as revenue
            FROM orders 
            WHERE status != 'cancelled'
            GROUP BY DATE(created_at)
            ORDER BY date DESC
            LIMIT 30
        """)
        daily_sales = cursor.fetchall()
        

        
        # Get top products by sales
        cursor.execute("""
            SELECT p.name_am as name, c.name_am as category,
                   COALESCE(SUM(oi.quantity), 0) as units_sold,
                   COALESCE(SUM(oi.quantity * oi.price_at_time), 0) as revenue
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            LEFT JOIN order_items oi ON p.id = oi.product_id
            GROUP BY p.id
            ORDER BY units_sold DESC
            LIMIT 10
        """)
        top_products = cursor.fetchall()
        top_products_list = [dict(tp) for tp in top_products] if top_products else []

        orders_list = []
        for order in orders:
            od = dict(order)
            od['customer_name'] = od.pop('full_name', None) or 'Guest'
            od['date'] = (od.get('created_at') or '')[:10]
            orders_list.append(od)

        daily_sales_list = [dict(sale) for sale in daily_sales] if daily_sales else []
        chart_labels = [s.get('date', '') for s in daily_sales_list]
        chart_values = [s.get('revenue') or 0 for s in daily_sales_list]
        
        return render_template('admin/reports/sales.html',
                               orders=orders_list,
                               total_revenue=total_revenue,
                               total_orders=total_orders,
                               avg_order_value=round(avg_order_value, 2),
                               daily_sales=daily_sales_list,
                               top_products=top_products_list,
                               chart_labels=chart_labels,
                               chart_values=chart_values,
                               start_date=start_date,
                               end_date=end_date,
                               lang=lang,
                               t=t)
                               
    except Exception as e:
        app.logger.error(f"Error loading sales report: {str(e)}")
        flash('Error loading sales report.', 'error')
        return redirect(url_for('admin_reports'))


@app.route('/admin/reports/products')
@admin_required
@limiter.exempt
def admin_reports_products():
    """Products performance report."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get products with sales data
        cursor.execute("""
            SELECT p.*, c.name as category_name,
                   COALESCE(SUM(oi.quantity), 0) as total_sold,
                   COALESCE(SUM(oi.quantity * oi.price_at_time), 0) as total_revenue
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            LEFT JOIN order_items oi ON p.id = oi.product_id
            WHERE p.is_active = 1
            GROUP BY p.id
            ORDER BY total_sold DESC, p.id DESC
        """)
        products = cursor.fetchall()
        
        # Get summary stats
        cursor.execute("""
            SELECT 
                COUNT(*) as total_products,
                SUM(stock_quantity) as total_stock,
                SUM(CASE WHEN stock_quantity = 0 THEN 1 ELSE 0 END) as out_of_stock,
                SUM(CASE WHEN stock_quantity <= low_stock_threshold AND stock_quantity > 0 THEN 1 ELSE 0 END) as low_stock,
                AVG(price) as avg_price,
                SUM(price * stock_quantity) as total_value
            FROM products
            WHERE is_active = 1
        """)
        stats = cursor.fetchone()

        # Get active categories and product counts
        cursor.execute("""
            SELECT c.id, c.name_am, c.name, COUNT(p.id) as product_count
            FROM categories c
            LEFT JOIN products p ON p.category_id = c.id AND p.is_active = 1
            WHERE c.is_active = 1
            GROUP BY c.id
            ORDER BY c.name_am ASC
        """)
        categories = cursor.fetchall()
        

        
        products_list = [dict(p) for p in products] if products else []
        stats_dict = dict(stats) if stats else {}
        categories_list = [dict(cat) for cat in categories] if categories else []
        
        return render_template('admin/reports/products.html',
                               products=products_list,
                               stats=stats_dict,
                               categories=categories_list,
                               total_products=stats_dict.get('total_products', 0),
                               total_value=stats_dict.get('total_value', 0),
                               lang=lang,
                               t=t)
                               
    except Exception as e:
        app.logger.error(f"Error loading products report: {str(e)}")
        flash('Error loading products report.', 'error')
        return redirect(url_for('admin_reports'))


# ==================== 23. ADMIN SETTINGS ====================

@app.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
@limiter.exempt
def admin_settings():
    """Admin settings page."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    conn = get_db()
    cursor = conn.cursor()
    
    # Get current settings
    cursor.execute("SELECT key, value FROM settings")
    settings_rows = cursor.fetchall()
    settings = {row[0]: row[1] for row in settings_rows}
    
    if request.method == 'POST':
        try:
            # General Settings
            site_name = request.form.get('site_name', 'Ethiosadat Furniture')
            site_description = request.form.get('site_description', '')
            admin_email = request.form.get('admin_email', 'admin@ethiosadat.com')
            
            # Contact Settings
            whatsapp_number = request.form.get('whatsapp_number', '251906020606')
            phone_number = request.form.get('phone_number', '+251906020606')
            store_address = request.form.get('store_address', 'Addis Ababa, Ethiopia')
            
            # Shipping Settings
            free_shipping_threshold = request.form.get('free_shipping_threshold', '5000')
            shipping_cost = request.form.get('shipping_cost', '200')
            
            # Localization
            currency = request.form.get('currency', 'ETB')
            default_language = request.form.get('default_language', 'am')
            
            # SEO Settings
            meta_keywords = request.form.get('meta_keywords', '')
            google_analytics = request.form.get('google_analytics', '')
            
            # Save settings
            settings_data = [
                ('site_name', site_name),
                ('site_description', site_description),
                ('admin_email', admin_email),
                ('whatsapp_number', whatsapp_number),
                ('phone_number', phone_number),
                ('store_address', store_address),
                ('free_shipping_threshold', free_shipping_threshold),
                ('shipping_cost', shipping_cost),
                ('currency', currency),
                ('default_language', default_language),
                ('meta_keywords', meta_keywords),
                ('google_analytics', google_analytics)
            ]
            
            for key, value in settings_data:
                cursor.execute("""
                    INSERT INTO settings (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = ?
                """, (key, value, value))
            
            conn.commit()
            
            # Update environment variables
            os.environ['WHATSAPP_NUMBER'] = whatsapp_number
            os.environ['FREE_SHIPPING_THRESHOLD'] = free_shipping_threshold
            os.environ['SHIPPING_COST'] = shipping_cost
            
            app.logger.info("Admin settings updated")
            flash('Settings saved successfully!', 'success')
            
        except Exception as e:
            app.logger.error(f"Error saving settings: {str(e)}")
            flash('Error saving settings.', 'error')
        

        return redirect(url_for('admin_settings'))
    

    
    return render_template('admin/settings.html',
                           settings=settings,
                           lang=lang,
                           t=t)


@app.route('/admin/settings/change-password', methods=['POST'])
@admin_required
def admin_change_password():
    """Change admin password."""
    current_password = request.form.get('current_password', '').strip()
    new_password = request.form.get('new_password', '').strip()
    confirm_password = request.form.get('confirm_password', '').strip()

    if not current_password or not new_password or not confirm_password:
        flash('All password fields are required.', 'error')
        return redirect(url_for('admin_settings') + '#change-password')

    if new_password != confirm_password:
        flash('New password and confirmation do not match.', 'error')
        return redirect(url_for('admin_settings') + '#change-password')

    if len(new_password) < 8:
        flash('New password must be at least 8 characters long.', 'error')
        return redirect(url_for('admin_settings') + '#change-password')

    conn = get_db()
    cursor = conn.cursor()
    try:
        admin_id = session.get('admin_id') or session.get('user_id')
        cursor.execute("SELECT id, password_hash FROM users WHERE id = ? AND is_admin = 1", (admin_id,))
        admin = cursor.fetchone()

        if not admin or not check_password_hash(admin[1], current_password):
            flash('Current password is incorrect.', 'error')
            return redirect(url_for('admin_settings') + '#change-password')

        new_hash = generate_password_hash(new_password, method='pbkdf2:sha256')
        cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, admin[0]))
        conn.commit()
        app.logger.info(f"Admin password changed for user id={admin[0]}")
        flash('Password changed successfully!', 'success')
    except Exception as e:
        app.logger.error(f"Error changing admin password: {str(e)}")
        flash('Error changing password. Please try again.', 'error')

    return redirect(url_for('admin_settings') + '#change-password')


@app.route('/admin/clear-cache', methods=['GET', 'POST'])
@admin_required
def admin_clear_cache():
    """Clear application cache."""
    try:
        # Clear session cart cache
        session.pop('cart', None)
        
        # Clear template cache
        app.jinja_env.cache = {}
        
        app.logger.info("Cache cleared by admin")
        return jsonify({'success': True, 'message': 'Cache cleared successfully'})
        
    except Exception as e:
        app.logger.error(f"Error clearing cache: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/backup-database', methods=['GET', 'POST'])
@admin_required
def admin_backup_database():
    """Create database backup."""
    try:
        import shutil
        db_path = Config.DATABASE_PATH if hasattr(Config, 'DATABASE_PATH') else 'ethiosadat.db'
        
        if not os.path.exists(db_path):
            return jsonify({'success': False, 'error': 'Database not found'}), 404
        
        backup_dir = 'backups'
        os.makedirs(backup_dir, exist_ok=True)
        
        timestamp = datetime_.datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_filename = f"ethiosadat_backup_{timestamp}.db"
        backup_path = os.path.join(backup_dir, backup_filename)
        
        shutil.copy2(db_path, backup_path)
        
        app.logger.info(f"Database backup created: {backup_filename}")
        
        return send_from_directory(backup_dir, backup_filename, as_attachment=True)
        
    except Exception as e:
        app.logger.error(f"Error backing up database: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== 24. ADMIN NOTIFICATIONS ====================

@app.route('/admin/send-notification', methods=['GET', 'POST'])
@admin_required
def admin_send_notification():
    """Send push notifications to users."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    if request.method == 'POST':
        try:
            title = request.form.get('title', '').strip()
            title_am = request.form.get('title_am', '').strip()
            title_ar = request.form.get('title_ar', '').strip()
            body = request.form.get('body', '').strip()
            body_am = request.form.get('body_am', '').strip()
            body_ar = request.form.get('body_ar', '').strip()
            image_url = request.form.get('image_url', '').strip()
            link = request.form.get('link', '').strip()
            target = request.form.get('target', 'all')
            
            if not title or not body:
                flash('Please enter both title and message', 'error')
                return redirect(url_for('admin_send_notification'))
            
            # Save notification to database
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO notifications (
                    title, title_am, title_ar,
                    body, body_am, body_ar,
                    image, link, target_audience,
                    sent_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                RETURNING id
            """, (title, title_am, title_ar, body, body_am, body_ar, image_url, link, target, session.get('admin_username', 'admin')))

            row = cursor.fetchone()
            notification_id = row['id'] if row else None
            conn.commit()

            
            # Send push notification via Firebase
            send_push_notification(title, body, image_url, link, target)
            
            app.logger.info(f"Notification sent: {title} (Target: {target})")
            flash('Notification sent successfully!', 'success')
            
        except Exception as e:
            app.logger.error(f"Error sending notification: {str(e)}")
            flash('Error sending notification.', 'error')
        
        return redirect(url_for('admin_send_notification'))
    
    return render_template('admin/send_notification.html', lang=lang, t=t)


def send_push_notification(title, body, image_url, link, target):
    """Send push notification using Firebase HTTP API."""
    try:
        # This is a placeholder for Firebase Cloud Messaging integration
        # You'll need to set up Firebase project and get API keys
        fcm_api_key = os.environ.get('FCM_API_KEY', '')
        
        if not fcm_api_key:
            app.logger.warning("FCM API key not configured. Notification not sent.")
            return
        
        # Implement FCM HTTP v1 API call here
        app.logger.info(f"Push notification would be sent to {target} users")
        
    except Exception as e:
        app.logger.error(f"Error sending push notification: {str(e)}")


# ==================== 25. API ROUTES ====================

@app.route('/api/cart/count')
def api_cart_count():
    """API endpoint to get current cart item count."""
    try:
        cart = session.get('cart', {})
        count = sum(cart.values()) if isinstance(cart, dict) else 0
        
        if session.get('user_id'):
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT SUM(quantity) FROM cart_items WHERE user_id = ?", (session['user_id'],))
            result = cursor.fetchone()
            count = result[0] or 0

        
        return jsonify({'success': True, 'cart_count': count})
        
    except Exception as e:
        app.logger.error(f"API error fetching cart count: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/cart')
def api_get_cart():
    """API endpoint to get current cart contents."""
    try:
        cart_items = []
        subtotal = 0
        
        android_user = is_android_app()

        if session.get('user_id'):
            conn = get_db()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT ci.*, p.name, p.name_am, p.name_ar, p.price, p.compare_price, p.thumbnail
                FROM cart_items ci
                JOIN products p ON ci.product_id = p.id
                WHERE ci.user_id = ?
            """, (session['user_id'],))

            rows = cursor.fetchall()

            for row in rows:
                discounted_price = row['price'] * 0.9 if (session.get('user_id') and android_user) else row['price']
                item_subtotal = discounted_price * row['quantity']
                subtotal += item_subtotal
                cart_items.append({
                    'product_id': row['product_id'],
                    'name': row['name'],
                    'name_am': row['name_am'],
                    'name_ar': row['name_ar'],
                    'price': row['price'],
                    'discounted_price': round(discounted_price, 2),
                    'quantity': row['quantity'],
                    'thumbnail': row['thumbnail'],
                    'subtotal': round(item_subtotal, 2)
                })
        else:
            cart = session.get('cart', {})
            if cart:
                conn = get_db()
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                placeholders = ','.join(['?'] * len(cart))
                cursor.execute(f"""
                    SELECT id, name, name_am, name_ar, price, compare_price, thumbnail
                    FROM products WHERE id IN ({placeholders}) AND is_active = 1
                """, list(cart.keys()))
                
                products = cursor.fetchall()

                
                for p in products:
                    quantity = cart.get(str(p['id']), 0)
                    if quantity > 0:
                        item_subtotal = p['price'] * quantity
                        subtotal += item_subtotal
                        cart_items.append({
                            'product_id': p['id'],
                            'name': p['name'],
                            'name_am': p['name_am'],
                            'name_ar': p['name_ar'],
                            'price': p['price'],
                            'discounted_price': p['price'],
                            'quantity': quantity,
                            'thumbnail': p['thumbnail'],
                            'subtotal': round(item_subtotal, 2)
                        })
        
        android_discount_active = bool(session.get('user_id') and android_user)
        discount = subtotal * 0.1 if android_discount_active else 0
        discount_message = (
            'Congratulations! You have received a 10% discount for being a registered user on our Android App.'
            if android_discount_active else ''
        )
        subtotal_after_discount = subtotal - discount

        threshold = int(os.environ.get('FREE_SHIPPING_THRESHOLD', '5000'))
        shipping_cost = 0 if subtotal_after_discount >= threshold else int(os.environ.get('SHIPPING_COST', '200'))
        total = subtotal_after_discount + shipping_cost
        count = len(cart_items)

        return jsonify({
            'success': True,
            'items': cart_items,
            'count': count,
            'subtotal': round(subtotal, 2),
            'discount': round(discount, 2),
            'discount_message': discount_message,
            'android_discount': android_discount_active,
            'shipping_cost': shipping_cost,
            'total': round(total, 2)
        })
        
    except Exception as e:
        app.logger.error(f"API error fetching cart: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/cart/add', methods=['POST'])
def api_cart_add():
    """API endpoint to add item to cart."""
    try:
        data = request.get_json()
        product_id = data.get('product_id')
        quantity = int(data.get('quantity', 1))
        
        if not product_id:
            return jsonify({'success': False, 'error': 'Product ID required'}), 400
        
        # Check product exists and has stock
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, name_am, price, stock_quantity FROM products WHERE id = ? AND is_active = 1", (product_id,))
        product = cursor.fetchone()
        
        if not product:

            return jsonify({'success': False, 'error': 'Product not found'}), 404
        
        if product[4] < quantity:

            return jsonify({'success': False, 'error': f'Only {product[4]} items available'}), 400
        
        if session.get('user_id'):
            cursor.execute("""
                SELECT id, quantity FROM cart_items 
                WHERE user_id = ? AND product_id = ?
            """, (session['user_id'], product_id))
            
            existing = cursor.fetchone()
            
            if existing:
                new_quantity = existing[1] + quantity
                if product[4] >= new_quantity:
                    cursor.execute("UPDATE cart_items SET quantity = ? WHERE id = ?", (new_quantity, existing[0]))
                else:

                    return jsonify({'success': False, 'error': f'Only {product[4]} items available'}), 400
            else:
                cursor.execute("""
                    INSERT INTO cart_items (user_id, product_id, quantity)
                    VALUES (?, ?, ?)
                """, (session['user_id'], product_id, quantity))
            
            conn.commit()
        else:
            cart = session.get('cart', {})
            cart_key = str(product_id)
            current_quantity = cart.get(cart_key, 0)
            new_quantity = current_quantity + quantity
            
            if product[4] >= new_quantity:
                cart[cart_key] = new_quantity
                session['cart'] = cart
                session.modified = True
            else:

                return jsonify({'success': False, 'error': f'Only {product[4]} items available'}), 400
        

        
        # Get updated cart count
        cart_count = get_cart_count()
        
        return jsonify({
            'success': True,
            'message': f'{product[1] or product[2]} added to cart',
            'cart_count': cart_count
        })
        
    except Exception as e:
        app.logger.error(f"API error adding to cart: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/cart/update', methods=['POST'])
def api_cart_update():
    """API endpoint to update cart item quantity."""
    try:
        data = request.get_json()
        product_id = data.get('product_id')
        quantity = int(data.get('quantity', 1))
        
        if not product_id:
            return jsonify({'success': False, 'error': 'Product ID required'}), 400
        
        if quantity < 1:
            return api_cart_remove()
        
        # Check stock
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT stock_quantity FROM products WHERE id = ?", (product_id,))
        product = cursor.fetchone()
        
        if product and quantity > product[0]:

            return jsonify({'success': False, 'error': f'Only {product[0]} items available'}), 400
        
        if session.get('user_id'):
            cursor.execute("""
                UPDATE cart_items SET quantity = ?
                WHERE user_id = ? AND product_id = ?
            """, (quantity, session['user_id'], product_id))
            conn.commit()
        else:
            cart = session.get('cart', {})
            cart[str(product_id)] = quantity
            session['cart'] = cart
            session.modified = True
        

        
        return jsonify({'success': True, 'message': 'Cart updated'})
        
    except Exception as e:
        app.logger.error(f"API error updating cart: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/cart/remove', methods=['POST'])
def api_cart_remove():
    """API endpoint to remove item from cart."""
    try:
        data = request.get_json()
        product_id = data.get('product_id')
        
        if not product_id:
            return jsonify({'success': False, 'error': 'Product ID required'}), 400
        
        if session.get('user_id'):
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM cart_items 
                WHERE user_id = ? AND product_id = ?
            """, (session['user_id'], product_id))
            conn.commit()

        else:
            cart = session.get('cart', {})
            cart_key = str(product_id)
            if cart_key in cart:
                del cart[cart_key]
            session['cart'] = cart
            session.modified = True
        
        return jsonify({'success': True, 'message': 'Item removed from cart'})
        
    except Exception as e:
        app.logger.error(f"API error removing from cart: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
# ==================== 26. API ROUTES (CONTINUED) ====================

@app.route('/api/products')
@limiter.limit("1000 per minute")
def api_products():
    """API endpoint to get all products with pagination and filtering."""
    try:
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)
        category_id = request.args.get('category_id', type=int)
        search = request.args.get('search', '').strip()
        min_price = request.args.get('min_price', type=float)
        max_price = request.args.get('max_price', type=float)
        sort_by = request.args.get('sort_by', 'newest')
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Build query
        query = """
            SELECT p.*, c.name as category_name, c.name_am as category_name_am
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.is_active = 1
        """
        count_query = "SELECT COUNT(*) FROM products p WHERE p.is_active = 1"
        params = []
        
        if category_id:
            query += " AND p.category_id = ?"
            count_query += " AND p.category_id = ?"
            params.append(category_id)
        
        if search:
            query += " AND (p.name LIKE ? OR p.name_am LIKE ? OR p.name_ar LIKE ?)"
            count_query += " AND (p.name LIKE ? OR p.name_am LIKE ? OR p.name_ar LIKE ?)"
            search_term = f'%{search}%'
            params.extend([search_term, search_term, search_term])
        
        if min_price is not None:
            query += " AND p.price >= ?"
            count_query += " AND p.price >= ?"
            params.append(min_price)
        
        if max_price is not None:
            query += " AND p.price <= ?"
            count_query += " AND p.price <= ?"
            params.append(max_price)
        
        # Get total count
        cursor.execute(count_query, params)
        total = cursor.fetchone()[0]
        
        # Add sorting
        if sort_by == 'price_asc':
            query += " ORDER BY p.price ASC"
        elif sort_by == 'price_desc':
            query += " ORDER BY p.price DESC"
        elif sort_by == 'name_asc':
            query += " ORDER BY p.name ASC"
        elif sort_by == 'popularity':
            query += " ORDER BY p.sales_count DESC"
        else:  # newest
            query += " ORDER BY p.id DESC"
        
        query += " LIMIT ? OFFSET ?"
        params.extend([per_page, (page - 1) * per_page])
        
        cursor.execute(query, params)
        products = cursor.fetchall()

        
        products_list = []
        for p in products:
            products_list.append({
                'id': p['id'],
                'name': p['name'] or '',
                'name_am': p['name_am'] or '',
                'name_ar': p['name_ar'] or '',
                'description': p['description'] or '',
                'description_am': p['description_am'] or '',
                'description_ar': p['description_ar'] or '',
                'price': p['price'],
                'compare_price': p['compare_price'],
                'stock_quantity': p['stock_quantity'] or 0,
                'thumbnail': p['thumbnail'] or '',
                'category_id': p['category_id'],
                'category_name': p['category_name'] if 'category_name' in p.keys() else None,
                'is_featured': p['is_featured'] or 0,
                'is_new': p['is_new'] or 0,
                'rating': 0
            })
        
        return jsonify({
            'success': True,
            'products': products_list,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page
        })
        
    except Exception as e:
        app.logger.error(f"API error fetching products: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/product/<int:product_id>')
def api_get_product(product_id):
    """API endpoint to get single product details."""
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT p.*, c.name as category_name, c.name_am as category_name_am
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.id = ? AND p.is_active = 1
        """, (product_id,))
        
        product = cursor.fetchone()

        
        if not product:
            return jsonify({'success': False, 'error': 'Product not found'}), 404
        
        product_dict = dict(product)
        
        # Parse images if stored as JSON
        images = []
        if product_dict.get('images'):
            try:
                images = json.loads(product_dict['images'])
            except:
                images = [product_dict.get('thumbnail', '')] if product_dict.get('thumbnail') else []
        
        # Calculate discount percentage
        discount = None
        if product_dict.get('compare_price') and product_dict['compare_price'] > product_dict['price']:
            discount = int(((product_dict['compare_price'] - product_dict['price']) / product_dict['compare_price']) * 100)
        
        return jsonify({
            'success': True,
            'product': {
                'id': product_dict['id'],
                'name': product_dict['name'],
                'name_am': product_dict['name_am'],
                'name_ar': product_dict['name_ar'],
                'description': product_dict['description'],
                'description_am': product_dict['description_am'],
                'description_ar': product_dict['description_ar'],
                'price': product_dict['price'],
                'compare_price': product_dict['compare_price'],
                'discount': discount,
                'stock_quantity': product_dict['stock_quantity'],
                'thumbnail': product_dict['thumbnail'],
                'images': images,
                'category_id': product_dict['category_id'],
                'category_name': product_dict['category_name'],
                'category_name_am': product_dict['category_name_am'],
                'material': product_dict.get('material', ''),
                'color': product_dict.get('color', ''),
                'dimensions': product_dict.get('dimensions', ''),
                'weight': product_dict.get('weight'),
                'is_featured': bool(product_dict.get('is_featured', 0)),
                'is_new': bool(product_dict.get('is_new', 0)),
                'rating': product_dict.get('rating', 0),
                'reviews': product_dict.get('reviews', 0)
            }
        })
        
    except Exception as e:
        app.logger.error(f"API error fetching product {product_id}: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/categories')
def api_categories():
    """API endpoint to get all categories."""
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT c.*, COUNT(p.id) as product_count
            FROM categories c
            LEFT JOIN products p ON p.category_id = c.id AND p.is_active = 1
            WHERE c.is_active = 1
            GROUP BY c.id
            ORDER BY c.sort_order ASC
        """)
        
        categories = cursor.fetchall()

        
        categories_list = [dict(cat) for cat in categories] if categories else []
        
        return jsonify({
            'success': True,
            'categories': categories_list
        })
        
    except Exception as e:
        app.logger.error(f"API error fetching categories: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/search')
@limiter.limit("20000 per minute")
def api_search_products():
    """API endpoint to search products."""
    try:
        query = request.args.get('q', '').strip()
        limit = request.args.get('limit', 10, type=int)
        
        if not query:
            return jsonify({'success': True, 'products': [], 'count': 0})
        
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        search_pattern = f'%{query}%'
        cursor.execute("""
            SELECT p.id, p.name, p.name_am, p.name_ar, p.price, p.compare_price, p.thumbnail,
                   c.name as category_name
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.is_active = 1 
            AND (p.name LIKE ? OR p.name_am LIKE ? OR p.name_ar LIKE ?)
            ORDER BY p.id DESC
            LIMIT ?
        """, (search_pattern, search_pattern, search_pattern, limit))
        
        products = cursor.fetchall()

        
        products_list = [dict(p) for p in products] if products else []
        
        return jsonify({
            'success': True,
            'products': products_list,
            'count': len(products_list),
            'query': query
        })
        
    except Exception as e:
        app.logger.error(f"API error searching products: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/branches')
def api_branches():
    """API endpoint to get all branches."""
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM branches 
            WHERE is_active = 1 
            ORDER BY sort_order ASC
        """)
        
        branches = cursor.fetchall()

        
        branches_list = []
        for branch in branches:
            branch_dict = dict(branch)
            branch_dict['maps_url'] = f"https://www.google.com/maps/dir/?api=1&destination={branch_dict['latitude']},{branch_dict['longitude']}"
            branches_list.append(branch_dict)
        
        return jsonify({
            'success': True,
            'branches': branches_list,
            'count': len(branches_list)
        })
        
    except Exception as e:
        app.logger.error(f"API error fetching branches: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/submit-order', methods=['POST'])
@limiter.limit("50000 per minute")
def api_submit_order():
    """API endpoint to submit order via AJAX."""
    try:
        data = request.get_json()
        
        if not session.get('user_id'):
            return jsonify({'success': False, 'error': 'Please login to place order'}), 401
        
        shipping_address = data.get('shipping_address', '').strip()
        shipping_phone = data.get('shipping_phone', '').strip()
        notes = data.get('notes', '').strip()
        
        if not shipping_address or not shipping_phone:
            return jsonify({'success': False, 'error': 'Shipping address and phone are required'}), 400
        
        # Get cart items
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ci.*, p.price, p.name, p.name_am
            FROM cart_items ci
            JOIN products p ON ci.product_id = p.id
            WHERE ci.user_id = ?
        """, (session['user_id'],))
        
        cart_items = cursor.fetchall()
        
        if not cart_items:

            return jsonify({'success': False, 'error': 'Cart is empty'}), 400
        
        # Calculate totals
        subtotal = 0
        items_list = []
        for item in cart_items:
            discounted_price = item[5] * 0.9  # 10% discount
            item_subtotal = discounted_price * item[4]
            subtotal += item_subtotal
            items_list.append({
                'product_id': item[1],
                'quantity': item[4],
                'price': discounted_price
            })
        
        discount = subtotal * 0.1
        subtotal_after_discount = subtotal - discount
        
        threshold = int(os.environ.get('FREE_SHIPPING_THRESHOLD', '5000'))
        shipping_cost = 0 if subtotal_after_discount >= threshold else int(os.environ.get('SHIPPING_COST', '200'))
        total = subtotal_after_discount + shipping_cost
        
        # Generate order number
        order_number = generate_order_number()
        
        # Create order
        cursor.execute("""
            INSERT INTO orders (
                order_number, user_id, status, payment_status,
                subtotal, discount, shipping_fee, total,
                shipping_address, shipping_phone, notes,
                created_at, updated_at
            ) VALUES (?, ?, 'pending', 'pending', ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            RETURNING id
        """, (order_number, session['user_id'], subtotal, discount, shipping_cost, total,
              shipping_address, shipping_phone, notes))

        row = cursor.fetchone()
        order_id = row['id'] if row else None
        
        # Create order items and update stock
        for item in items_list:
            cursor.execute("""
                INSERT INTO order_items (order_id, product_id, quantity, price_at_time)
                VALUES (?, ?, ?, ?)
            """, (order_id, item['product_id'], item['quantity'], item['price']))
            
            cursor.execute("""
                UPDATE products SET 
                    stock_quantity = stock_quantity - ?,
                    sales_count = sales_count + ?
                WHERE id = ?
            """, (item['quantity'], item['quantity'], item['product_id']))
        
        # Clear cart
        cursor.execute("DELETE FROM cart_items WHERE user_id = ?", (session['user_id'],))
        
        conn.commit()

        
        # Send WhatsApp notification
        whatsapp_url = send_order_whatsapp(
            session.get('user_name', 'Customer'),
            shipping_phone,
            items_list,
            total,
            order_number
        )
        
        app.logger.info(f"Order placed via API: {order_number} by user {session['user_id']}")
        
        return jsonify({
            'success': True,
            'message': 'Order placed successfully!',
            'order_id': order_id,
            'order_number': order_number,
            'total': total,
            'whatsapp_url': whatsapp_url
        })
        
    except Exception as e:
        app.logger.error(f"API error submitting order: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== 27. TEMPLATE FILTERS ====================

@app.template_filter('truncate_text')
def truncate_filter(text, length=50):
    """Truncate text to specified length."""
    if not text:
        return ''
    if len(text) <= length:
        return text
    return text[:length].rstrip() + '...'


@app.template_filter('format_datetime')
def datetime_filter(dt):
    """Format datetime for display."""
    if not dt:
        return ''
    if isinstance(dt, str):
        try:
            dt = datetime_.datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except:
            return dt
    return dt.strftime('%Y-%m-%d %H:%M')


@app.template_filter('format_date')
def format_date_filter(date_obj, format_type='short'):
    """Format date for display."""
    if not date_obj:
        return ''
    
    if isinstance(date_obj, str):
        try:
            date_obj = datetime_.datetime.fromisoformat(date_obj.replace('Z', '+00:00'))
        except:
            return date_obj
    
    if format_type == 'short':
        return date_obj.strftime('%d/%m/%Y')
    elif format_type == 'long':
        return date_obj.strftime('%B %d, %Y')
    else:
        return date_obj.strftime('%Y-%m-%d %H:%M:%S')


@app.template_filter('nl2br')
def nl2br_filter(text):
    """Convert newlines to HTML line breaks."""
    if not text:
        return ''
    return text.replace('\n', '<br>')


@app.template_filter('default_value')
def default_filter(value, default_value=''):
    """Return default value if the primary value is empty/None."""
    if value is None or value == '':
        return default_value
    return value


@app.template_filter('currency_symbol')
def currency_symbol_filter(price):
    """Return only the currency symbol (ETB)."""
    return "ETB"


@app.template_filter('safe_text')
def safe_text_filter(text):
    """Escape HTML special characters for safe display."""
    if not text:
        return ''
    import html
    return html.escape(str(text))


@app.template_filter('from_json')
def from_json_filter(json_str):
    """Parse JSON string to Python object."""
    if not json_str:
        return []
    try:
        return json.loads(json_str)
    except Exception:
        return []


@app.template_filter('to_json')
def to_json_filter(obj):
    """Convert Python object to JSON string."""
    try:
        return json.dumps(obj)
    except Exception:
        return '{}'


@app.template_filter('discount_percent')
def discount_percent_filter(price, compare_price):
    """Calculate discount percentage."""
    try:
        if compare_price and compare_price > price:
            return int(((compare_price - price) / compare_price) * 100)
        return 0
    except (ValueError, TypeError):
        return 0


# ==================== 28. CLI COMMANDS ====================

@app.cli.command('init-db')
def init_db_command():
    """Initialize the database with all required tables."""
    import sqlite3
    from database.db import init_db
    
    print("Initializing database...")
    init_db()
    print("Database initialized successfully!")


@app.cli.command('seed-demo')
def seed_demo_data():
    """Seed the database with demo products and categories."""
    print("Seeding demo data...")
    
    conn = get_db()
    cursor = conn.cursor()
    
    # Check if categories exist
    cursor.execute("SELECT COUNT(*) FROM categories")
    if cursor.fetchone()[0] == 0:
        categories = [
            ('ሶፋ', 'Sofa', 'صوفا', '🛋️', 1),
            ('አልጋ', 'Bed', 'سرير', '🛏️', 2),
            ('መጅሊስ', 'Mejlis', 'مجلس', '🪑', 3),
            ('መጋረጃ', 'Curtain', 'ستارة', '🚪', 4),
            ('ቁምሳጥን', 'Wardrobe', 'خزانة', '🗄️', 5),
            ('ሌላ', 'Other', 'آخر', '📦', 6)
        ]
        for cat in categories:
            cursor.execute("""
                INSERT INTO categories (name_am, name, name_ar, icon, sort_order, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
            """, cat)
        print("Categories added")
    
    # Check if products exist
    cursor.execute("SELECT COUNT(*) FROM products")
    if cursor.fetchone()[0] == 0:
        demo_products = [
            ('ምቹ ሶፋ', 'Comfort Sofa', 'أريكة مريحة', 12500, 15000, 'sofa1.jpg', 1, 1),
            ('ዘመናዊ አልጋ', 'Modern Bed', 'سرير حديث', 18500, 22000, 'bed1.jpg', 2, 1),
            ('የቅንጦት መጅሊስ', 'Luxury Mejlis', 'مجلس فاخر', 22500, 28000, 'mejlis1.jpg', 3, 1),
            ('ዘመናዊ መጋረጃ', 'Modern Curtain', 'ستارة حديثة', 3500, 4500, 'curtain1.jpg', 4, 1),
            ('ትልቅ ቁምሳጥን', 'Large Wardrobe', 'خزانة كبيرة', 15500, 19000, 'wardrobe1.jpg', 5, 1)
        ]
        for prod in demo_products:
            cursor.execute("""
                INSERT INTO products (name_am, name, name_ar, price, compare_price, thumbnail, category_id, stock_quantity, is_featured, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 10, ?, 1)
            """, prod)
        print("Demo products added")
    
    conn.commit()

    print("Demo data seeding complete!")


@app.cli.command('list-products')
def list_products():
    """List all products in the database."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.id, p.name_am, p.name, p.price, c.name_am as category
        FROM products p
        LEFT JOIN categories c ON p.category_id = c.id
        WHERE p.is_active = 1
        ORDER BY p.id DESC
    """)
    products = cursor.fetchall()

    
    if not products:
        print("No products found.")
        return
    
    print("\n" + "=" * 80)
    print(f"{'ID':<5} {'Name (Amharic)':<30} {'Name (English)':<25} {'Price':<12} {'Category':<15}")
    print("-" * 80)
    
    for p in products:
        print(f"{p[0]:<5} {p[1][:28]:<30} {p[2][:23]:<25} {p[3]:<12} {p[4] if p[4] else 'N/A':<15}")
    
    print("=" * 80)
    print(f"Total products: {len(products)}")


@app.cli.command('show-stats')
def show_stats():
    """Show database statistics."""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM products WHERE is_active = 1")
    products = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM categories WHERE is_active = 1")
    categories = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_admin = 0")
    customers = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM orders")
    orders = cursor.fetchone()[0]
    
    cursor.execute("SELECT SUM(total) FROM orders WHERE status = 'delivered'")
    revenue = cursor.fetchone()[0] or 0
    
    cursor.execute("SELECT COUNT(*) FROM orders WHERE status = 'pending'")
    pending = cursor.fetchone()[0]
    

    
    print("\n" + "=" * 50)
    print("ETHIOSADAT DATABASE STATISTICS")
    print("=" * 50)
    print(f"Products:          {products}")
    print(f"Categories:        {categories}")
    print(f"Customers:         {customers}")
    print(f"Orders:            {orders}")
    print(f"Total Revenue:     {revenue:,.0f} ETB")
    print(f"Pending Orders:    {pending}")
    print("=" * 50)


@app.cli.command('create-admin')
def create_admin():
    """Create or update admin password (interactive)."""
    import getpass
    
    print("\nAdmin Setup")
    print("-" * 30)
    
    password = getpass.getpass("Enter admin password (min 4 characters): ")
    
    if len(password) < 4:
        print("Password must be at least 4 characters!")
        return
    
    confirm = getpass.getpass("Confirm admin password: ")
    
    if password != confirm:
        print("Passwords do not match!")
        return
    
    env_file = '.env'
    
    if os.path.exists(env_file):
        with open(env_file, 'r') as f:
            lines = f.readlines()
        
        admin_updated = False
        with open(env_file, 'w') as f:
            for line in lines:
                if line.startswith('ADMIN_PASSWORD='):
                    f.write(f'ADMIN_PASSWORD={password}\n')
                    admin_updated = True
                else:
                    f.write(line)
            
            if not admin_updated:
                f.write(f'\nADMIN_PASSWORD={password}\n')
        
        print("Admin password updated in .env file!")
    else:
        with open(env_file, 'w') as f:
            f.write(f'ADMIN_PASSWORD={password}\n')
            f.write('ADMIN_USERNAME=admin\n')
            f.write('SECRET_KEY=ethiosadat_default_secret_key_2026\n')
        print(".env file created with admin credentials!")
    
    print("Please restart the application for changes to take effect.")


# ==================== 29. MAINTENANCE MODE ====================

MAINTENANCE_MODE = os.environ.get('MAINTENANCE_MODE', 'False').lower() == 'true'


@app.before_request
def check_maintenance_mode():
    """Put the application in maintenance mode."""
    if MAINTENANCE_MODE:
        # Allow admin access and static files
        if request.path.startswith('/admin') or request.path == '/login':
            return None
        if request.path.startswith('/static'):
            return None
        if request.path == '/health':
            return None
        
        lang = get_lang()
        titles = {
            'am': 'በማሻሻል ላይ',
            'en': 'Under Maintenance',
            'ar': 'تحت الصيانة'
        }
        messages = {
            'am': 'በአሁን ሰዓት ማሻሻያ እየተደረገ ነው። እባክዎ ቆይተው ይመለሱ።',
            'en': 'We are currently performing maintenance. Please check back soon.',
            'ar': 'نقوم حاليًا بأعمال الصيانة. يرجى العودة قريبًا.'
        }
        
        title = titles.get(lang, titles['en'])
        message = messages.get(lang, messages['en'])
        
        return f'''
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8"><title>503 - {title}</title>
        <style>
            *{{margin:0;padding:0;box-sizing:border-box;}}
            body{{font-family:Arial,sans-serif;background:linear-gradient(135deg,#1a73e8,#0d47a1);display:flex;justify-content:center;align-items:center;height:100vh;}}
            .box{{background:white;padding:40px;border-radius:20px;text-align:center;max-width:400px;}}
            h1{{color:#1a73e8;font-size:48px;margin:0 0 20px 0;}}
            p{{margin:20px 0;color:#666;}}
            .spinner{{width:40px;height:40px;margin:20px auto;border:4px solid #f3f3f3;border-top:4px solid #1a73e8;border-radius:50%;animation:spin 1s linear infinite;}}
            @keyframes spin{{0%{{transform:rotate(0deg);}}100%{{transform:rotate(360deg);}}}}
        </style>
        </head>
        <body><div class="box"><div class="spinner"></div><h1>{title}</h1><p>{message}</p></div></body>
        </html>
        ''', 503


# ==================== 30. APPLICATION SHUTDOWN HANDLER ====================

def shutdown_handler():
    """Cleanup operations when the application shuts down."""
    app.logger.info("Application shutting down...")


atexit.register(shutdown_handler)


# ==================== 31. MAIN ENTRY POINT ====================

def create_app():
    """Application factory pattern for creating the Flask app."""
    return app


def main():
    """Main entry point for running the application."""
    if len(sys.argv) > 1 and sys.argv[1] == 'init-db':
        init_db_command()
        return
    
    if len(sys.argv) > 1 and sys.argv[1] == 'seed':
        seed_demo_data()
        return
    
    debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '0.0.0.0')
    
    print("\n" + "=" * 60)
    print("ETHIOSADAT FURNITURE STORE")
    print("=" * 60)
    print("Starting application...")
    print(f"Host: {host}")
    print(f"Port: {port}")
    print(f"Debug Mode: {debug}")
    print("=" * 60)
    print(f"\nAdmin URL: http://{host}:{port}/login")
    print(f"WhatsApp: {WHATSAPP_NUMBER}")
    print("\n" + "=" * 60)
    print("Server is ready! Press CTRL+C to stop.")
    print("=" * 60 + "\n")
    
    app.run(host=host, port=port, debug=debug)


if __name__ == '__main__':
    main()

# ==================== END OF APPLICATION FILE ====================
# ==================== 32. USER-AGENT DETECTION & PLATFORM HANDLING ====================

@app.before_request
def detect_platform():
    """Detect user platform (Desktop, Mobile Browser, Android App) and store in g."""
    from middleware.platform import get_platform, is_android_app

    g.platform = get_platform()
    g.is_android_app = is_android_app()

    # Log platform for analytics
    if request.method == 'GET' and not request.path.startswith('/static'):
        app.logger.debug(f"Platform: {g.platform} - Path: {request.path}")

    # Track live visitors and total page views (non-static, non-API only)
    if not request.path.startswith('/static') and not request.path.startswith('/api/'):
        sid = session.get('_id')
        if not sid:
            sid = uuid.uuid4().hex
            session['_id'] = sid
        now = time.time()
        cutoff = now - 300  # 5-minute window
        with _visitor_lock:
            _active_visitors[sid] = now
            # Prune stale visitors
            stale = [k for k, v in _active_visitors.items() if v < cutoff]
            for k in stale:
                del _active_visitors[k]
        # Increment total page views counter in DB (best-effort, no crash on failure)
        try:
            _pv_conn = get_db()
            _pv_conn.execute(
                "INSERT INTO settings (key, value) VALUES ('page_views', '1') "
                "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
            )
            _pv_conn.commit()
            # Do NOT call _pv_conn.close() here — get_db() manages the
            # connection lifecycle via Flask's g; closing it here would
            # invalidate g.db for all subsequent route handlers.
        except Exception:
            pass


@app.context_processor
def inject_platform():
    """Inject platform information into all templates."""
    from middleware.platform import get_platform, is_android_app, is_desktop, is_mobile_browser
    
    return {
        'platform': get_platform(),
        'is_android_app': is_android_app(),
        'is_desktop': is_desktop(),
        'is_mobile_browser': is_mobile_browser(),
        'show_about_section': is_desktop() or is_mobile_browser()  # Hide on Android App
    }


@app.route('/api/platform')
def api_platform():
    """API endpoint to get platform information."""
    from middleware.platform import get_platform, is_android_app
    
    return jsonify({
        'success': True,
        'platform': get_platform(),
        'is_android_app': is_android_app(),
        'user_agent': request.headers.get('User-Agent', '')
    })


# ==================== 33. ORDER TRACKING ROUTES ====================

@app.route('/track-order/<order_number>')
def track_order(order_number):
    """Public order tracking page."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT o.*, u.full_name, u.email, u.phone
            FROM orders o
            LEFT JOIN users u ON o.user_id = u.id
            WHERE o.order_number = ?
        """, (order_number,))
        
        order = cursor.fetchone()

        
        if not order:
            flash('Order not found!', 'error')
            return redirect(url_for('index'))
        
        order_dict = dict(order)
        
        # Get order items
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT oi.*, p.name, p.name_am, p.name_ar, p.thumbnail
            FROM order_items oi
            JOIN products p ON oi.product_id = p.id
            WHERE oi.order_id = ?
        """, (order_dict['id'],))
        items = cursor.fetchall()

        
        items_list = [dict(item) for item in items] if items else []
        
        return render_template('customer/track_order.html',
                               order=order_dict,
                               items=items_list,
                               lang=lang,
                               t=t)
                               
    except Exception as e:
        app.logger.error(f"Order tracking error: {str(e)}")
        flash('Error loading order details', 'error')
        return redirect(url_for('index'))


# ==================== 34. WISHLIST ROUTES ====================

@app.route('/wishlist')
@user_login_required
def wishlist():
    """User wishlist page."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Create wishlist table if not exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wishlist (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, product_id)
            )
        """)
        conn.commit()
        
        cursor.execute("""
            SELECT w.*, p.name, p.name_am, p.name_ar, p.price, p.compare_price, p.thumbnail
            FROM wishlist w
            JOIN products p ON w.product_id = p.id
            WHERE w.user_id = ? AND p.is_active = 1
            ORDER BY w.created_at DESC
        """, (session['user_id'],))
        
        wishlist_items = cursor.fetchall()

        
        wishlist_list = [dict(item) for item in wishlist_items] if wishlist_items else []
        
        return render_template('customer/wishlist.html',
                               wishlist=wishlist_list,
                               lang=lang,
                               t=t)
                               
    except Exception as e:
        app.logger.error(f"Wishlist error: {str(e)}")
        flash('Error loading wishlist', 'error')
        return redirect(url_for('index'))


@app.route('/api/wishlist/add', methods=['POST'])
@user_login_required
def api_wishlist_add():
    """Add product to wishlist."""
    try:
        data = request.get_json()
        product_id = data.get('product_id')
        
        if not product_id:
            return jsonify({'success': False, 'error': 'Product ID required'}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Create table if not exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wishlist (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, product_id)
            )
        """)

        cursor.execute("""
            INSERT INTO wishlist (user_id, product_id)
            VALUES (?, ?)
            ON CONFLICT (user_id, product_id) DO NOTHING
        """, (session['user_id'], product_id))
        
        conn.commit()

        
        return jsonify({'success': True, 'message': 'Added to wishlist'})
        
    except Exception as e:
        app.logger.error(f"Wishlist add error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/wishlist/remove', methods=['POST'])
@user_login_required
def api_wishlist_remove():
    """Remove product from wishlist."""
    try:
        data = request.get_json()
        product_id = data.get('product_id')
        
        if not product_id:
            return jsonify({'success': False, 'error': 'Product ID required'}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM wishlist WHERE user_id = ? AND product_id = ?
        """, (session['user_id'], product_id))
        conn.commit()

        
        return jsonify({'success': True, 'message': 'Removed from wishlist'})
        
    except Exception as e:
        app.logger.error(f"Wishlist remove error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== 35. COUPON MANAGEMENT ====================

@app.route('/api/apply-coupon', methods=['POST'])
def api_apply_coupon():
    """Apply discount coupon to cart."""
    try:
        data = request.get_json()
        code = data.get('code', '').strip().upper()
        
        if not code:
            return jsonify({'success': False, 'error': 'Coupon code required'}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Create coupons table if not exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS coupons (
                id SERIAL PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                discount_type TEXT DEFAULT 'percentage',
                discount_value DOUBLE PRECISION NOT NULL,
                min_order DOUBLE PRECISION DEFAULT 0,
                max_discount DOUBLE PRECISION,
                valid_from TIMESTAMP,
                valid_to TIMESTAMP,
                usage_limit INTEGER,
                used_count INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        
        # Check if coupon exists and is valid
        cursor.execute("""
            SELECT * FROM coupons 
            WHERE code = ? AND is_active = 1
            AND (valid_from IS NULL OR valid_from <= CURRENT_TIMESTAMP)
            AND (valid_to IS NULL OR valid_to >= CURRENT_TIMESTAMP)
            AND (usage_limit IS NULL OR used_count < usage_limit)
        """, (code,))
        
        coupon = cursor.fetchone()
        
        if not coupon:

            return jsonify({'success': False, 'error': 'Invalid or expired coupon code'}), 400
        
        # Get cart subtotal
        subtotal = get_cart_total()
        
        if subtotal < coupon['min_order']:
            return jsonify({'success': False, 'error': f"Minimum order of {coupon['min_order']} ETB required"}), 400

        # Calculate discount
        if coupon['discount_type'] == 'percentage':
            discount = subtotal * (coupon['discount_value'] / 100)
            if coupon['max_discount']:
                discount = min(discount, coupon['max_discount'])
        else:
            discount = coupon['discount_value']

        # Store coupon in session
        session['applied_coupon'] = {
            'code': code,
            'discount': discount,
            'coupon_id': coupon['id']
        }
        session.modified = True
        

        
        return jsonify({
            'success': True,
            'message': f'Coupon applied! You saved {discount:.2f} ETB',
            'discount': discount
        })
        
    except Exception as e:
        app.logger.error(f"Apply coupon error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== 36. REVIEWS & RATINGS ====================

@app.route('/api/product/<int:product_id>/reviews', methods=['GET', 'POST'])
def product_reviews(product_id):
    """Get or add product reviews."""
    if request.method == 'GET':
        try:
            conn = get_db()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT r.*, u.full_name as user_name
                FROM reviews r
                JOIN users u ON r.user_id = u.id
                WHERE r.product_id = ? AND r.is_approved = 1
                ORDER BY r.created_at DESC
                LIMIT 20
            """, (product_id,))
            
            reviews = cursor.fetchall()

            
            reviews_list = [dict(review) for review in reviews] if reviews else []
            
            # Calculate average rating
            avg_rating = sum(r['rating'] for r in reviews_list) / len(reviews_list) if reviews_list else 0
            
            return jsonify({
                'success': True,
                'reviews': reviews_list,
                'average_rating': round(avg_rating, 1),
                'total_reviews': len(reviews_list)
            })
            
        except Exception as e:
            app.logger.error(f"Get reviews error: {str(e)}")
            return jsonify({'success': False, 'error': str(e)}), 500
    
    else:  # POST - Add review
        if not session.get('user_id'):
            return jsonify({'success': False, 'error': 'Please login to leave a review'}), 401
        
        try:
            data = request.get_json()
            rating = data.get('rating', 0)
            comment = data.get('comment', '').strip()
            
            if rating < 1 or rating > 5:
                return jsonify({'success': False, 'error': 'Rating must be between 1 and 5'}), 400
            
            if not comment:
                return jsonify({'success': False, 'error': 'Please write a review'}), 400
            
            conn = get_db()
            cursor = conn.cursor()
            
            # Create reviews table if not exists
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reviews (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    rating INTEGER NOT NULL,
                    comment TEXT NOT NULL,
                    is_approved INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            conn.commit()
            
            # Check if user already reviewed this product
            cursor.execute("""
                SELECT id FROM reviews WHERE product_id = ? AND user_id = ?
            """, (product_id, session['user_id']))
            
            if cursor.fetchone():

                return jsonify({'success': False, 'error': 'You have already reviewed this product'}), 400
            
            cursor.execute("""
                INSERT INTO reviews (product_id, user_id, rating, comment, is_approved)
                VALUES (?, ?, ?, ?, 0)
            """, (product_id, session['user_id'], rating, comment))
            
            conn.commit()

            
            app.logger.info(f"New review added for product {product_id} by user {session['user_id']}")
            
            return jsonify({'success': True, 'message': 'Review submitted! Awaiting approval.'})
            
        except Exception as e:
            app.logger.error(f"Add review error: {str(e)}")
            return jsonify({'success': False, 'error': str(e)}), 500


# ==================== 37. NEWSLETTER SUBSCRIPTION ====================

@app.route('/api/subscribe-newsletter', methods=['POST'])
def subscribe_newsletter():
    """Subscribe email to newsletter."""
    try:
        data = request.get_json()
        email = data.get('email', '').strip().lower()
        
        if not email:
            return jsonify({'success': False, 'error': 'Email is required'}), 400
        
        if not re.match(r'^[^\s@]+@([^\s@.,]+\.)+[^\s@.,]{2,}$', email):
            return jsonify({'success': False, 'error': 'Invalid email address'}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS newsletter (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                subscribed_at TIMESTAMP DEFAULT NOW(),
                is_active INTEGER DEFAULT 1
            )
        """)
        conn.commit()

        cursor.execute("""
            INSERT INTO newsletter (email) VALUES (?)
            ON CONFLICT (email) DO NOTHING
        """, (email,))
        
        conn.commit()

        
        return jsonify({'success': True, 'message': 'Successfully subscribed to newsletter!'})
        
    except Exception as e:
        app.logger.error(f"Newsletter subscription error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== 38. ADMIN REVIEW MANAGEMENT ====================

@app.route('/admin/reviews')
@admin_required
def admin_reviews():
    """Admin reviews management page."""
    lang = get_lang()
    t = TEXTS.get(lang, TEXTS['am'])
    
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT r.*, p.name_am as product_name, u.full_name as user_name
            FROM reviews r
            JOIN products p ON r.product_id = p.id
            JOIN users u ON r.user_id = u.id
            ORDER BY r.created_at DESC
        """)
        
        reviews = cursor.fetchall()

        
        reviews_list = [dict(review) for review in reviews] if reviews else []
        
        return render_template('admin/reviews/index.html',
                               reviews=reviews_list,
                               lang=lang,
                               t=t)
                               
    except Exception as e:
        app.logger.error(f"Admin reviews error: {str(e)}")
        flash('Error loading reviews', 'error')
        return redirect(url_for('admin_dashboard'))


@app.route('/admin/reviews/approve/<int:review_id>', methods=['POST'])
@admin_required
def admin_approve_review(review_id):
    """Approve a review."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE reviews SET is_approved = 1 WHERE id = ?", (review_id,))
        conn.commit()

        
        return jsonify({'success': True, 'message': 'Review approved'})
        
    except Exception as e:
        app.logger.error(f"Approve review error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/reviews/delete/<int:review_id>', methods=['DELETE'])
@admin_required
def admin_delete_review(review_id):
    """Delete a review."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
        conn.commit()

        
        return jsonify({'success': True, 'message': 'Review deleted'})
        
    except Exception as e:
        app.logger.error(f"Delete review error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== 39. ROBOTS.TXT & SITEMAP ====================

@app.route('/robots.txt')
def robots_txt():
    """Serve robots.txt for search engines."""
    content = """User-agent: *
Allow: /
Disallow: /admin/
Disallow: /login/
Disallow: /logout/
Disallow: /cart/clear/
Sitemap: https://ethiosadat.com/sitemap.xml
"""
    return make_response(content, 200, {'Content-Type': 'text/plain'})


@app.route('/sitemap.xml')
def sitemap_xml():
    """Generate sitemap.xml for search engines."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("SELECT id, updated_at FROM products WHERE is_active = 1 ORDER BY id DESC")
        products = cursor.fetchall()
        
        cursor.execute("SELECT id, created_at FROM categories WHERE is_active = 1")
        categories = cursor.fetchall()
        

        
        base_url = request.url_root.rstrip('/')
        current_date = datetime_.datetime.now().strftime('%Y-%m-%d')
        
        xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
        xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        
        # Static pages
        static_pages = ['/', '/products', '/about', '/contact', '/branches', '/faq', '/shipping-info', '/returns']
        for page in static_pages:
            xml += f'  <url>\n    <loc>{base_url}{page}</loc>\n    <lastmod>{current_date}</lastmod>\n    <changefreq>weekly</changefreq>\n    <priority>0.8</priority>\n  </url>\n'
        
        # Categories
        for cat in categories:
            cat_id = cat[0]
            xml += f'  <url>\n    <loc>{base_url}/category/{cat_id}</loc>\n    <lastmod>{current_date}</lastmod>\n    <changefreq>weekly</changefreq>\n    <priority>0.7</priority>\n  </url>\n'
        
        # Products
        for prod in products:
            prod_id = prod[0]
            lastmod = prod[1].split()[0] if prod[1] else current_date
            xml += f'  <url>\n    <loc>{base_url}/product/{prod_id}</loc>\n    <lastmod>{lastmod}</lastmod>\n    <changefreq>monthly</changefreq>\n    <priority>0.9</priority>\n  </url>\n'
        
        xml += '</urlset>'
        
        return make_response(xml, 200, {'Content-Type': 'application/xml'})
        
    except Exception as e:
        app.logger.error(f"Sitemap error: {str(e)}")
        return make_response('Error generating sitemap', 500)
# ==================== 40. CACHING & PERFORMANCE ====================

# Configure cache
cache_config = {
    'CACHE_TYPE': os.environ.get('CACHE_TYPE', 'SimpleCache'),
    'CACHE_DEFAULT_TIMEOUT': int(os.environ.get('CACHE_DEFAULT_TIMEOUT', 300)),
    'CACHE_THRESHOLD': 1000,
    'CACHE_IGNORE_ERRORS': True
}

# Try to use Redis if available
if os.environ.get('REDIS_URL'):
    cache_config['CACHE_TYPE'] = 'RedisCache'
    cache_config['CACHE_REDIS_URL'] = os.environ.get('REDIS_URL')

class _NoOpCache:
    """Simple no-op cache stub (flask-caching not installed)."""
    def __init__(self):
        self.config = cache_config
    def clear(self): pass
    def get(self, key): return None
    def set(self, key, value, timeout=None): pass
    def cached(self, timeout=None, key_prefix=None):
        def decorator(f): return f
        return decorator

cache = _NoOpCache()


@app.route('/api/cache/clear')
@admin_required
def api_clear_cache():
    """Clear all cache (admin only)."""
    try:
        cache.clear()
        app.jinja_env.cache = {}
        app.logger.info("Cache cleared by admin")
        return jsonify({'success': True, 'message': 'Cache cleared successfully'})
    except Exception as e:
        app.logger.error(f"Cache clear error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


def get_cached_categories():
    """Get categories (no-op cache stub)."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM categories WHERE is_active = 1 ORDER BY sort_order")
        categories = cursor.fetchall()

        return [dict(cat) for cat in categories] if categories else []
    except Exception as e:
        app.logger.error(f"Error getting cached categories: {str(e)}")
        return []


# ==================== 41. RESPONSE COMPRESSION ====================

@app.after_request
def compress_response(response):
    """Add cache-control headers for API responses."""
    if request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


# ==================== 42. ADDITIONAL SECURITY HEADERS ====================

@app.after_request
def add_csp_headers(response):
    """Add Content Security Policy headers."""
    # Only add CSP for HTML pages
    if 'text/html' in response.headers.get('Content-Type', ''):
        csp = {
            'default-src': "'self'",
            'script-src': "'self' 'unsafe-inline' 'unsafe-eval' https://translate.google.com https://cdn.jsdelivr.net",
            'style-src': "'self' 'unsafe-inline'",
            'img-src': "'self' data: https:",
            'font-src': "'self'",
            'connect-src': "'self'",
            'frame-src': "https://translate.google.com",
        }
        
        csp_string = '; '.join([f"{key} {value}" for key, value in csp.items()])
        response.headers['Content-Security-Policy'] = csp_string
    
    return response


# ==================== 43. DATABASE CONNECTION POOLING ====================

def get_db_connection():
    """Get database connection from pool."""
    try:
        conn = get_db()
        return conn
    except Exception as e:
        app.logger.error(f"Database connection error: {str(e)}")
        raise


@app.teardown_appcontext
def close_db_connection(exception=None):
    """Close database connection at the end of request."""
    try:
        from database.db import close_db
        close_db(exception)
    except Exception as e:
        app.logger.error(f"Error closing database connection: {str(e)}")


# ==================== 44. REQUEST TIMING & MONITORING ====================

import time

@app.before_request
def start_request_timer():
    """Start timer for request monitoring."""
    request.start_time = time.time()


@app.after_request
def log_request_performance(response):
    """Log request performance for slow requests."""
    if hasattr(request, 'start_time'):
        elapsed = time.time() - request.start_time
        if elapsed > 1.0:  # Log requests taking more than 1 second
            app.logger.warning(f"Slow request: {request.method} {request.path} - {elapsed:.2f}s")
            
            # Add timing header
            response.headers['X-Response-Time'] = f"{elapsed:.3f}s"
    
    return response


# ==================== 45. ERROR NOTIFICATION ====================

def send_error_notification(error_message, error_detail):
    """Send error notification to admin via WhatsApp."""
    try:
        if os.environ.get('ERROR_NOTIFICATIONS', 'False').lower() == 'true':
            error_text = f"⚠️ *Error on Ethiosadat*\n\n"
            error_text += f"Message: {error_message}\n"
            error_text += f"Path: {request.path}\n"
            error_text += f"Time: {datetime_.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            error_text += f"IP: {request.remote_addr}\n"
            
            encoded = urllib.parse.quote(error_text)
            whatsapp_url = f"https://wa.me/{WHATSAPP_NUMBER}?text={encoded}"
            
            # Log instead of sending to avoid recursion
            app.logger.info(f"Error notification would be sent: {error_message}")
            
    except Exception as e:
        app.logger.error(f"Error sending notification: {str(e)}")


# ==================== 46. HEALTH CHECK DETAILS ====================

@app.route('/health/details')
@admin_required
def health_check_details():
    """Detailed health check for monitoring (admin only)."""
    health_status = {
        'status': 'healthy',
        'timestamp': datetime_.datetime.now().isoformat(),
        'version': '2.0.0',
        'system': {
            'python_version': sys.version,
            'platform': sys.platform,
            'uptime': time.time() - app.start_time if hasattr(app, 'start_time') else None
        },
        'services': {},
        'cache': {}
    }
    
    # Database check
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        health_status['services']['database'] = 'connected'

    except Exception as e:
        health_status['services']['database'] = f'error: {str(e)}'
        health_status['status'] = 'unhealthy'
    
    # Cache check
    try:
        cache.set('health_check_test', 'ok', timeout=10)
        test_value = cache.get('health_check_test')
        health_status['services']['cache'] = 'working' if test_value == 'ok' else 'failed'
    except Exception as e:
        health_status['services']['cache'] = f'error: {str(e)}'
    
    # Storage check
    try:
        upload_dir = app.config['UPLOAD_FOLDER']
        if os.path.exists(upload_dir):
            health_status['services']['storage'] = 'accessible'
        else:
            health_status['services']['storage'] = 'not_found'
    except Exception as e:
        health_status['services']['storage'] = f'error: {str(e)}'
    
    # Get cache stats
    health_status['cache']['type'] = cache.config.get('CACHE_TYPE', 'unknown')
    health_status['cache']['default_timeout'] = cache.config.get('CACHE_DEFAULT_TIMEOUT', 300)
    
    status_code = 200 if health_status['status'] == 'healthy' else 503
    return jsonify(health_status), status_code


# ==================== 47. TRANSLATION MANAGEMENT ROUTES ====================

@app.route('/admin/translations/status', methods=['GET'])
@admin_required
def translation_status():
    """Get translation cache statistics (admin only)."""
    stats = get_translation_stats()
    return jsonify({
        'status': 'success',
        'cache': stats,
        'fallback_languages': list(FALLBACK_TEXTS.keys()),
        'supported_languages': ['am', 'en', 'ar']
    })


@app.route('/admin/translations/clear', methods=['POST'])
@admin_required
def clear_translations():
    """Clear translation cache (admin only)."""
    try:
        clear_translation_cache()
        app.logger.info("Translation cache cleared by admin")
        return jsonify({
            'status': 'success',
            'message': 'Translation cache cleared successfully'
        })
    except Exception as e:
        app.logger.error(f"Error clearing translation cache: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f'Error clearing cache: {str(e)}'
        }), 500


@app.route('/api/translate', methods=['POST'])
def api_translate():
    """
    API endpoint to translate text.
    Requires: text (str), target_lang (str)
    """
    try:
        data = request.get_json()
        text = data.get('text', '').strip()
        target_lang = data.get('target_lang', 'en')
        
        if not text:
            return jsonify({'error': 'Text is required'}), 400
        
        translated = translate_text(text, target_lang)
        
        return jsonify({
            'status': 'success',
            'original': text,
            'translated': translated,
            'language': target_lang
        })
    
    except Exception as e:
        app.logger.error(f"Translation API error: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/translate-batch', methods=['POST'])
def api_translate_batch():
    """
    API endpoint to translate multiple texts at once.
    Requires: texts (list), target_lang (str)
    """
    try:
        data = request.get_json()
        texts = data.get('texts', [])
        target_lang = data.get('target_lang', 'en')
        
        if not texts or not isinstance(texts, list):
            return jsonify({'error': 'Texts array is required'}), 400
        
        translations = batch_translate(texts, target_lang)
        
        return jsonify({
            'status': 'success',
            'translations': translations,
            'language': target_lang,
            'count': len(translations)
        })
    
    except Exception as e:
        app.logger.error(f"Batch translation API error: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


# ==================== 48. APP STARTUP TIMESTAMP ====================

app.start_time = time.time()


# ==================== 49. DATABASE BACKUP SCHEDULER (Optional) ====================

import threading

def scheduled_backup():
    """Run scheduled database backup every day at midnight."""
    while True:
        try:
            now = datetime_.datetime.now()
            # Calculate time until next midnight
            next_midnight = datetime_.datetime(now.year, now.month, now.day) + datetime_.timedelta(days=1)
            sleep_seconds = (next_midnight - now).total_seconds()
            
            time.sleep(sleep_seconds)
            
            # Perform backup
            if os.environ.get('AUTO_BACKUP', 'False').lower() == 'true':
                import shutil
                db_path = Config.DATABASE_PATH if hasattr(Config, 'DATABASE_PATH') else 'ethiosadat.db'
                
                if os.path.exists(db_path):
                    backup_dir = 'backups'
                    os.makedirs(backup_dir, exist_ok=True)
                    
                    timestamp = datetime_.datetime.now().strftime('%Y%m%d')
                    backup_filename = f"ethiosadat_backup_{timestamp}.db"
                    backup_path = os.path.join(backup_dir, backup_filename)
                    
                    shutil.copy2(db_path, backup_path)
                    app.logger.info(f"Scheduled backup created: {backup_filename}")
                    
                    # Delete old backups (keep last 7 days)
                    for f in os.listdir(backup_dir):
                        if f.startswith('ethiosadat_backup_') and f != backup_filename:
                            file_path = os.path.join(backup_dir, f)
                            file_time = datetime_.datetime.fromtimestamp(os.path.getctime(file_path))
                            if (datetime_.datetime.now() - file_time).days > 7:
                                os.remove(file_path)
                                app.logger.info(f"Deleted old backup: {f}")
                                
        except Exception as e:
            app.logger.error(f"Scheduled backup error: {str(e)}")
            time.sleep(3600)  # Wait an hour before retrying


# Start backup thread if enabled
if os.environ.get('AUTO_BACKUP', 'False').lower() == 'true':
    backup_thread = threading.Thread(target=scheduled_backup, daemon=True)
    backup_thread.start()
    app.logger.info("Scheduled backup thread started")


# ==================== 49. RATE LIMIT ERROR HANDLER ====================

@app.errorhandler(429)
def ratelimit_exceeded(error):
    """Handle rate limit exceeded errors."""
    app.logger.warning(f"Rate limit exceeded: {request.remote_addr} - {request.path}")
    lang = get_lang()
    
    messages = {
        'am': 'በጣም ብዙ ጥያቄዎችን በአጭር ጊዜ ውስጥ ልከዋል። እባክዎ ቆይተው ይሞክሩ።',
        'en': 'Too many requests. Please try again later.',
        'ar': 'عدد كبير جدًا من الطلبات. يرجى المحاولة مرة أخرى لاحقًا.'
    }
    message = messages.get(lang, messages['en'])
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': False, 'error': message}), 429
    
    return f'''
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><title>429 - Too Many Requests</title>
    <style>
        *{{margin:0;padding:0;box-sizing:border-box;}}
        body{{font-family:Arial,sans-serif;background:linear-gradient(135deg,#ff9800,#f57c00);display:flex;justify-content:center;align-items:center;height:100vh;}}
        .box{{background:white;padding:40px;border-radius:20px;text-align:center;max-width:400px;}}
        h1{{color:#ff9800;font-size:80px;margin:0;}}
        p{{margin:20px 0;color:#666;}}
        a{{display:inline-block;background:#ff9800;color:white;padding:12px 24px;text-decoration:none;border-radius:30px;}}
    </style>
    </head>
    <body><div class="box"><h1>429</h1><p>{message}</p><a href="/">Home</a></div></body>
    </html>
    ''', 429


# ==================== 50. FINAL MIDDLEWARE & INITIALIZATION ====================

@app.before_request
def set_secure_headers():
    """Set additional security headers before request."""
    # Prevent clickjacking
    response = make_response()
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    
    # Prevent MIME sniffing
    response.headers['X-Content-Type-Options'] = 'nosniff'
    
    # Enable XSS protection
    response.headers['X-XSS-Protection'] = '1; mode=block'


def initialize_app():
    """Initialize application settings and data."""
    app.logger.info("Initializing Ethiosadat Furniture Application")
    
    # Initialize database
    try:
        init_db()
        app.logger.info("Database initialized successfully")
    except Exception as e:
        app.logger.error(f"Database initialization error: {str(e)}")
    
    # Create required directories
    directories = [
        'logs',
        'backups',
        'static/uploads',
        'static/uploads/products',
        'static/uploads/ads',
        'static/images',
        'static/css',
        'static/js'
    ]
    
    for directory in directories:
        os.makedirs(directory, exist_ok=True)
    
    app.logger.info("Application initialized successfully")


# Run initialization
with app.app_context():
    initialize_app()


# ==================== 51. EXPORTED FUNCTIONS FOR TESTS ====================

# Make functions available for testing
if __name__ != '__main__':
    # For testing purposes
    app.testing = True


# ==================== 52. COMPLETE APPLICATION ====================

# Register error handlers
def register_error_handlers():
    """Register all error handlers."""
    @app.errorhandler(404)
    def not_found(error):
        return jsonify({'success': False, 'error': 'Resource not found'}), 404
    
    @app.errorhandler(405)
    def method_not_allowed(error):
        return jsonify({'success': False, 'error': 'Method not allowed'}), 405


register_error_handlers()


# ==================== 53. APPLICATION READY ====================

# ==================== CUSTOMER BLUEPRINT ALIASES ====================
# Templates use 'customer.*' endpoint names — register them as aliases
_customer_endpoint_map = {
    'customer.index': index,
    'customer.products': products,
    'customer.about': about,
    'customer.contact': contact,
    'customer.search': search,
    'customer.faq': faq,
    'customer.shipping_info': shipping_info,
    'customer.returns_policy': returns_policy,
    'customer.terms': terms,
    'customer.privacy': privacy,
    'customer.user_login': user_login,
    'customer.user_register': user_register,
    'customer.user_logout': user_logout,
    'customer.user_profile': user_profile,
    'customer.update_profile': update_profile,
    'customer.change_password': change_password,
    'customer.delete_account': delete_account,
    'customer.user_orders': user_orders,
    'customer.forgot_password': forgot_password,
}
for _alias, _fn in _customer_endpoint_map.items():
    app.view_functions[_alias] = _fn

app.logger.info("=" * 60)
app.logger.info("ETHIOSADAT FURNITURE APPLICATION READY")
app.logger.info(f"Environment: {os.environ.get('FLASK_ENV', 'production')}")
app.logger.info(f"Debug Mode: {app.debug}")
app.logger.info(f"Cache Type: {cache.config.get('CACHE_TYPE', 'disabled')}")
app.logger.info("=" * 60)


# ==================== 54. MAIN ENTRY POINT (Final) ====================

def main():
    """Main entry point for running the application."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Ethiosadat Furniture Application')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to')
    parser.add_argument('--port', type=int, default=5000, help='Port to bind to')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    parser.add_argument('--init-db', action='store_true', help='Initialize database')
    parser.add_argument('--seed', action='store_true', help='Seed demo data')
    
    args = parser.parse_args()
    
    if args.init_db:
        init_db_command()
        return
    
    if args.seed:
        seed_demo_data()
        return
    
    print("\n" + "=" * 60)
    print("🪑 ETHIOSADAT FURNITURE STORE")
    print("=" * 60)
    print(f"Starting server at http://{args.host}:{args.port}")
    print(f"Admin Panel: http://{args.host}:{args.port}/login")
    print(f"Debug Mode: {args.debug}")
    print("=" * 60)
    print("\nPress CTRL+C to stop the server\n")
    
    app.run(host=args.host, port=args.port, debug=args.debug)


# ==================== END OF APPLICATION FILE ====================