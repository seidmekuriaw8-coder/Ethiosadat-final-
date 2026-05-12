"""
Database Module for Ethiosadat Furniture
PostgreSQL backend using psycopg2 with a sqlite3-compatible adapter layer.
"""

import os
import psycopg2
import psycopg2.extras
from flask import g

# በፎቶው ላይ ባለው መረጃ መሰረት የተስተካከለ ዳታቤዝ URL
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://postgres:admin123@localhost:5432/ethiosadat_db')

class _PsycopgCursor:
    """Wraps a psycopg2 DictCursor to provide a sqlite3-compatible cursor API."""

    def __init__(self, real_cursor):
        self._c = real_cursor
        self._lastrowid = None

    def execute(self, query, params=None):
        q = query.replace('?', '%s').strip()
        if params is not None:
            self._c.execute(q, params)
        else:
            self._c.execute(q)
        
        # INSERT ከሆነ lastrowid ን ለመያዝ (PostgreSQL RETURNING id ያስፈልገዋል)
        if "RETURNING id" in q.upper() or "INSERT" in q.upper():
            try:
                row = self._c.fetchone()
                if row:
                    self._lastrowid = row[0]
            except:
                pass
        return self

    def executemany(self, query, params_list):
        q = query.replace('?', '%s').strip()
        self._c.executemany(q, params_list)
        return self

    def fetchall(self):
        return self._c.fetchall()

    def fetchone(self):
        return self._c.fetchone()

    def __iter__(self):
        return iter(self._c)

    @property
    def lastrowid(self):
        return self._lastrowid

    @lastrowid.setter
    def lastrowid(self, value):
        self._lastrowid = value

    @property
    def rowcount(self):
        return self._c.rowcount

    @property
    def description(self):
        return self._c.description


class _PsycopgConn:
    """Wraps a psycopg2 connection to provide a sqlite3-compatible connection API."""

    def __init__(self, raw_conn):
        self._conn = raw_conn

    def execute(self, query, params=None):
        cur = _PsycopgCursor(self._conn.cursor())
        return cur.execute(query, params)

    def cursor(self):
        return _PsycopgCursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, value):
        pass


def _raw_connect():
    """Open a raw psycopg2 connection. በምስሉ ላይ ባለው መረጃ መሰረት ተስተካክሏል።"""
    try:
        return psycopg2.connect(
            dbname="ethiosadat_db",
            user="postgres",
            password="admin123", # image_fe2601.png ላይ በሰጠኸው መረጃ መሰረት
            host="localhost",
            port="5432",
            cursor_factory=psycopg2.extras.DictCursor
        )
    except Exception as e:
        # ካልሰራ በ URL ለመሞከር
        return psycopg2.connect(
            DATABASE_URL,
            cursor_factory=psycopg2.extras.DictCursor
        )


def get_db():
    """Return the per-request wrapped database connection (cached on g)."""
    try:
        if 'db' not in g:
            g.db = _PsycopgConn(_raw_connect())
        return g.db
    except (RuntimeError, Exception):
        return _PsycopgConn(_raw_connect())


def close_db(e=None):
    """Close the database connection at the end of the request."""
    wrapped = g.pop('db', None)
    if wrapped is not None:
        try:
            wrapped.close()
        except Exception:
            pass


def init_db():
    """Create all tables and seed default data (PostgreSQL DDL)."""
    conn = _raw_connect()
    cur = conn.cursor()

    # Tables Creation
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT,
            phone TEXT,
            address TEXT,
            city TEXT,
            is_admin SMALLINT DEFAULT 0,
            is_active SMALLINT DEFAULT 1,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            last_login TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            name_am TEXT,
            name_ar TEXT,
            description TEXT,
            icon TEXT,
            image TEXT,
            sort_order INTEGER DEFAULT 0,
            is_active SMALLINT DEFAULT 1,
            parent_id INTEGER
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            name_am TEXT,
            name_ar TEXT,
            name_en TEXT,
            description TEXT,
            description_am TEXT,
            description_ar TEXT,
            description_en TEXT,
            price DOUBLE PRECISION NOT NULL,
            compare_price DOUBLE PRECISION,
            cost DOUBLE PRECISION,
            sku TEXT UNIQUE,
            barcode TEXT,
            stock_quantity INTEGER DEFAULT 0,
            low_stock_threshold INTEGER DEFAULT 5,
            images TEXT,
            thumbnail TEXT,
            is_active SMALLINT DEFAULT 1,
            is_featured SMALLINT DEFAULT 0,
            is_new SMALLINT DEFAULT 0,
            weight DOUBLE PRECISION,
            dimensions TEXT,
            material TEXT,
            color TEXT,
            views INTEGER DEFAULT 0,
            sales_count INTEGER DEFAULT 0,
            category_id INTEGER NOT NULL,
            meta_title TEXT,
            meta_description TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS cart_items (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER DEFAULT 1,
            added_at TIMESTAMP DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            order_number TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            payment_status TEXT DEFAULT 'pending',
            payment_method TEXT,
            subtotal DOUBLE PRECISION NOT NULL,
            discount DOUBLE PRECISION DEFAULT 0,
            shipping_fee DOUBLE PRECISION DEFAULT 0,
            total DOUBLE PRECISION NOT NULL,
            shipping_address TEXT NOT NULL,
            shipping_city TEXT,
            shipping_phone TEXT,
            notes TEXT,
            tracking_number TEXT,
            estimated_delivery DATE,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id SERIAL PRIMARY KEY,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            price_at_time DOUBLE PRECISION NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS advertisements (
            id SERIAL PRIMARY KEY,
            title TEXT,
            title_am TEXT,
            title_ar TEXT,
            description TEXT,
            description_am TEXT,
            description_ar TEXT,
            image TEXT NOT NULL,
            link TEXT,
            sort_order INTEGER DEFAULT 0,
            is_active SMALLINT DEFAULT 1,
            start_date TIMESTAMP DEFAULT NOW(),
            end_date TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS branches (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            name_am TEXT,
            name_ar TEXT,
            address TEXT NOT NULL,
            address_am TEXT,
            address_ar TEXT,
            phone TEXT,
            email TEXT,
            latitude DOUBLE PRECISION NOT NULL,
            longitude DOUBLE PRECISION NOT NULL,
            working_hours TEXT,
            image TEXT,
            sort_order INTEGER DEFAULT 0,
            is_active SMALLINT DEFAULT 1
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            title_am TEXT,
            title_ar TEXT,
            body TEXT NOT NULL,
            body_am TEXT,
            body_ar TEXT,
            image TEXT,
            link TEXT,
            target_audience TEXT DEFAULT 'all',
            sent_count INTEGER DEFAULT 0,
            sent_at TIMESTAMP DEFAULT NOW(),
            created_by INTEGER
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Indexes
    cur.execute("CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_products_featured ON products(is_featured)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_products_active ON products(is_active)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_number ON orders(order_number)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cart_user ON cart_items(user_id)")

    # Seed Default Data
    cur.execute("SELECT COUNT(*) FROM categories")
    if cur.fetchone()[0] == 0:
        defaults = [
            ('ሶፋ', 'Sofa', 'صوفا', '🛋️', 1),
            ('አልጋ', 'Bed', 'سرير', '🛏️', 2),
            ('መጅሊስ', 'Mejlis', 'مجلس', '🪑', 3),
            ('መጋረጃ', 'Curtain', 'ستارة', '🚪', 4),
            ('ቁምሳጥን', 'Wardrobe', 'خزانة', '🗄️', 5),
            ('ሌላ', 'Other', 'آخر', '📦', 6),
        ]
        cur.executemany(
            "INSERT INTO categories (name_am, name, name_ar, icon, sort_order) VALUES (%s, %s, %s, %s, %s)",
            defaults
        )
        print(f"✅ Seeded {len(defaults)} default categories")

    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        from werkzeug.security import generate_password_hash
        admin_hash = generate_password_hash('admin123456', method='pbkdf2:sha256')
        cur.execute(
            "INSERT INTO users (username, email, password_hash, full_name, is_admin, is_active) VALUES (%s, %s, %s, %s, %s, %s)",
            ('admin', 'admin@ethiosadat.com', admin_hash, 'Administrator', 1, 1)
        )
        print("✅ Default admin user created")

    cur.execute("SELECT COUNT(*) FROM settings")
    if cur.fetchone()[0] == 0:
        default_settings = [
            ('site_name', 'Ethiosadat Furniture'),
            ('site_name_am', 'ኢትዮሳዳት ቤት ዕቃ'),
            ('site_name_ar', 'إثيوصادات للأثاث'),
            ('site_email', 'info@ethiosadat.com'),
            ('site_phone', '+251906020606'),
            ('whatsapp_number', '251906020606'),
            ('free_shipping_threshold', '5000'),
            ('shipping_cost', '200'),
            ('currency', 'ETB'),
            ('default_language', 'am'),
        ]
        cur.executemany("INSERT INTO settings (key, value) VALUES (%s, %s)", default_settings)

    cur.execute("SELECT COUNT(*) FROM branches")
    if cur.fetchone()[0] == 0:
        branches = [
            ('መሀል መርካቶ ማርስ', 'ማርስ የገበያ ማእከል 2ኛ ፎቅ ሱቅ ቁጥር 230', 9.0100, 38.7450, '+251906020606', 1),
            ('ቤተል', 'ቢጫ ፎቅ ጎን', 9.0080, 38.7600, '+251906080606', 2),
            ('ፉሪ ኖክ', 'ኖክ ማደያ ፊት ለፊት', 8.9900, 38.7300, '+251906090606', 3),
            ('ድሬዳዋ መስቀለኛ', 'የሰይዶ ታክሲ ተራ ጋር', 9.5900, 41.8500, '+251906020606', 4),
            ('ድሬዳዋ ሞል', 'ቢራ ሞል 1ኛ ፎቅ', 9.5950, 41.8550, '+251906080606', 5),
            ('አሶሳ', 'የተባበሩት ማዲያ ፊትለፊት', 10.0700, 34.5300, '+251906090606', 6),
            ('ቡታጅራ', 'ቄጤማ መናኸሪያ ጎን', 8.1200, 38.3700, '+251906020606', 7),
            ('ሽሬ', 'ቶታል ማዲያ ፊትለፊት', 14.1000, 38.2800, '+251906080606', 8),
            ('ሰመራ', '', 11.7900, 41.0100, '+251906090606', 9),
        ]
        for b in branches:
            cur.execute(
                "INSERT INTO branches (name, address, latitude, longitude, phone, sort_order, is_active) VALUES (%s, %s, %s, %s, %s, %s, 1)",
                b
            )
        print(f"✅ Seeded {len(branches)} branches")

    conn.commit()
    cur.close()
    conn.close()
    print("✅ PostgreSQL database initialized successfully!")


def init_db_app(app):
    """Initialize database within a Flask app context."""
    with app.app_context():
        init_db()


def get_db_stats():
    """Return aggregate statistics for the admin dashboard."""
    try:
        db = get_db()
        cursor = db.cursor()
        stats = {}
        for table, key in [
            ('products', 'products'),
            ('advertisements', 'ads'),
            ('orders', 'orders'),
            ('categories', 'categories'),
            ('users', 'users'),
        ]:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            stats[key] = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM orders WHERE status = 'pending'")
        stats['pending_orders'] = cursor.fetchone()[0] or 0
        return stats
    except Exception as e:
        print(f"Error getting DB stats: {e}")
        return {'products': 0, 'ads': 0, 'orders': 0, 'categories': 0, 'users': 0, 'pending_orders': 0}


def commit_or_rollback(db=None):
    """Commit the current transaction or roll back on failure."""
    if db is None:
        db = get_db()
    try:
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        print(f"Transaction rolled back: {e}")
        return False


def test_connection():
    """Return True if the database is reachable."""
    try:
        conn = _raw_connect()
        conn.close()
        return True
    except Exception as e:
        print(f"Database connection failed: {e}")
        return False