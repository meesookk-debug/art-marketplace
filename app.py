import os
import hmac
import io
import urllib.request
import hashlib
import secrets
import tempfile
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime, timedelta
from functools import wraps

from dotenv import load_dotenv
load_dotenv()
load_dotenv(".env.local", override=False)

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from uuid import uuid4

try:
    from vercel import blob
except ImportError:
    blob = None

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
STAFF_ACCESS_KEY = os.getenv("STAFF_ACCESS_KEY", "")

# -----------------------------------------------------------------------------
# Database helper
# -----------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE,
    password_hash TEXT NOT NULL,
    full_name VARCHAR(100) NOT NULL,
    role VARCHAR(20) NOT NULL DEFAULT 'customer',
    artist_status VARCHAR(20) NOT NULL DEFAULT 'none',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artworks (
    id SERIAL PRIMARY KEY,
    title VARCHAR(150) NOT NULL,
    artist VARCHAR(100) NOT NULL,
    category VARCHAR(50) NOT NULL,
    price NUMERIC(12,2) NOT NULL CHECK (price >= 0),
    description TEXT NOT NULL,
    image_url TEXT NOT NULL,
    original_image_url TEXT,
    digital_file_url TEXT,
    tags TEXT NOT NULL DEFAULT '',
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    stock INTEGER NOT NULL DEFAULT 1 CHECK (stock >= 0),
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    total NUMERIC(12,2) NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'pending_payment',
    payment_method VARCHAR(30) NOT NULL DEFAULT 'promptpay',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    artwork_id INTEGER NOT NULL REFERENCES artworks(id),
    quantity INTEGER NOT NULL DEFAULT 1,
    unit_price NUMERIC(12,2) NOT NULL
);

CREATE TABLE IF NOT EXISTS likes (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    artwork_id INTEGER NOT NULL REFERENCES artworks(id) ON DELETE CASCADE,
    PRIMARY KEY(user_id, artwork_id)
);

CREATE TABLE IF NOT EXISTS follows (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    artist_name VARCHAR(100) NOT NULL,
    PRIMARY KEY(user_id, artist_name)
);

CREATE TABLE IF NOT EXISTS reviews (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    artwork_id INTEGER NOT NULL REFERENCES artworks(id),
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, artwork_id)
);

CREATE TABLE IF NOT EXISTS commission_requests (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES users(id),
    artist_name VARCHAR(100) NOT NULL,
    brief TEXT NOT NULL,
    budget NUMERIC(12,2) NOT NULL CHECK (budget >= 0),
    status VARCHAR(30) NOT NULL DEFAULT 'requested',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS logs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    action VARCHAR(100) NOT NULL,
    entity VARCHAR(50) NOT NULL,
    entity_id INTEGER,
    detail TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def get_db():
    if "db" not in g:
        if not psycopg2:
            raise RuntimeError("psycopg2-binary is required")
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        g.db = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    return g.db


def query(sql, params=(), fetchone=False, commit=False):
    db = get_db()
    with db.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchone() if fetchone else cur.fetchall()
    if commit:
        db.commit()
    return rows


def execute(sql, params=(), fetchone=False, fetchall=False):
    conn = get_db()

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)

            value = None

            if fetchall:
                value = cur.fetchall()
            elif fetchone:
                value = cur.fetchone()

            conn.commit()
            return value

    except Exception:
        if conn and not conn.closed:
            conn.rollback()
        raise

def init_db():
    db = get_db()
    with db.cursor() as cur:
        cur.execute(SCHEMA)
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255) UNIQUE")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS artist_bio TEXT")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS portfolio_url TEXT")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS artist_applied_at TIMESTAMP")
        cur.execute("ALTER TABLE artworks ADD COLUMN IF NOT EXISTS original_image_url TEXT")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS artist_status VARCHAR(20) NOT NULL DEFAULT 'none'")
        cur.execute("UPDATE users SET artist_status='none' WHERE artist_status IS NULL")
        cur.execute("UPDATE users SET artist_status='approved' WHERE role='artist' AND artist_status='none'")
        cur.execute("SELECT COUNT(*) AS n FROM users")
        if cur.fetchone()["n"] == 0:
            users = [
                ("admin", generate_password_hash("admin123"), "System Admin", "admin"),
                ("staff", generate_password_hash("staff123"), "Gallery Staff", "staff"),
                ("customer", generate_password_hash("customer123"), "Demo Customer", "customer"),
            ]
            for u in users:
                cur.execute(
                    "INSERT INTO users(username,password_hash,full_name,role) VALUES(%s,%s,%s,%s)", u
                )
            cur.execute("SELECT id FROM users WHERE username='staff'")
            staff_id = cur.fetchone()["id"]
            demo = [
                ("Moonlight Lake", "Narin Art", "Painting", 3500, "Acrylic landscape artwork.", "https://images.unsplash.com/photo-1500534623283-312aade485b7", "https://example.com/files/moonlight-lake.zip", "lake,landscape,blue", "approved", 1, staff_id),
                ("Blue Geometry", "Mali Studio", "Abstract", 4200, "Modern abstract composition.", "https://images.unsplash.com/photo-1549490349-8643362247b5", "https://example.com/files/blue-geometry.zip", "abstract,geometry,blue", "approved", 1, staff_id),
                ("Quiet Morning", "Narin Art", "Digital Art", 1800, "Digital artwork for collectors.", "https://images.unsplash.com/photo-1513364776144-60967b0f800f", "https://example.com/files/quiet-morning.zip", "digital,morning,soft", "approved", 1, staff_id),
            ]
            for a in demo:
                cur.execute(
                    """INSERT INTO artworks(title,artist,category,price,description,image_url,digital_file_url,tags,status,stock,created_by)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", a
                )
    db.commit()
    
@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)

    if db is not None and not db.closed:
        db.close()


def log_action(action, entity, entity_id=None, detail=""):
    user_id = session.get("user_id")
    execute(
        "INSERT INTO logs(user_id,action,entity,entity_id,detail) VALUES(%s,%s,%s,%s,%s)",
        (user_id, action, entity, entity_id, detail),
    )


def current_user():
    if "user_id" not in session:
        return None
    return query(
        "SELECT id,username,email,full_name,role,artist_status,artist_bio,portfolio_url,artist_applied_at FROM users WHERE id=%s",
        (session["user_id"],),
        True,
    )


@app.before_request
def load_user():
    g.user = current_user()
    g.lang = "en"


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user:
            flash("Please log in first.", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not g.user:
                return redirect(url_for("login"))
            if g.user["role"] not in roles:
                flash("You do not have permission to access this page.", "danger")
                return redirect(url_for("index"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


def staff_access_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user:
            return redirect(url_for("login", next=request.path))

        if g.user["role"] not in ("admin", "staff"):
            flash("You do not have permission to access this page.", "danger")
            return redirect(url_for("index"))

        if not STAFF_ACCESS_KEY:
            flash("Staff access key is not configured.", "danger")
            return redirect(url_for("index"))

        if session.get("staff_verified") is not True:
            return redirect(url_for("staff_access", next=request.path))

        return view(*args, **kwargs)

    return wrapped


def admin_access_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user:
            return redirect(url_for("login", next=request.path))

        if g.user["role"] != "admin":
            flash("You do not have permission to access this page.", "danger")
            return redirect(url_for("index"))

        if not STAFF_ACCESS_KEY:
            flash("Admin access key is not configured.", "danger")
            return redirect(url_for("index"))

        if session.get("staff_verified") is not True:
            return redirect(url_for("staff_access", next=request.path))

        return view(*args, **kwargs)

    return wrapped



def artist_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        if g.user["role"] != "artist":
            flash("Artist access is required.", "danger")
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/staff-access", methods=["GET", "POST"])
@login_required
def staff_access():
    if g.user["role"] not in ("admin", "staff"):
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for("index"))

    if request.method == "POST":
        access_key = request.form.get("access_key", "")
        attempts = session.get("staff_access_attempts", 0)

        if attempts >= 5:
            session.pop("staff_verified", None)
            flash("Too many failed attempts. Please log in again.", "danger")
            session.clear()
            return redirect(url_for("login"))

        if not STAFF_ACCESS_KEY or not hmac.compare_digest(access_key, STAFF_ACCESS_KEY):
            session["staff_access_attempts"] = attempts + 1
            remaining = 5 - session["staff_access_attempts"]
            if remaining <= 0:
                session.clear()
                flash("Too many failed attempts. Please log in again.", "danger")
                return redirect(url_for("login"))
            flash(f"Invalid access key. {remaining} attempt(s) remaining.", "danger")
            return render_template("staff_access.html")

        session["staff_verified"] = True
        session.pop("staff_access_attempts", None)

        next_url = request.args.get("next") or url_for("dashboard")
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = url_for("dashboard")

        return redirect(next_url)

    return render_template("staff_access.html")


def _require_blob():
    if blob is None:
        raise ValueError("Vercel Blob SDK is not installed. Please add 'vercel' to requirements.txt.")
    if not os.getenv("BLOB_READ_WRITE_TOKEN"):
        raise ValueError("BLOB_READ_WRITE_TOKEN is not configured.")
    return blob


def _upload_file_to_blob(local_path, blob_path, content_type):
    """Upload a local temporary file to the configured public Vercel Blob store."""
    blob_client = _require_blob()
    result = blob_client.upload_file(
        local_path,
        blob_path,
        access="public",
        content_type=content_type,
        add_random_suffix=False,
        overwrite=False,
        token=os.getenv("BLOB_READ_WRITE_TOKEN"),
    )
    return result.url


def _build_watermarked_preview(image, artist_name):
    """Create a watermarked preview in /tmp and upload it to Vercel Blob."""
    image = image.convert("RGBA")
    max_side = 1800
    scale = min(1, max_side / max(image.size))
    if scale < 1:
        image = image.resize(
            (int(image.width * scale), int(image.height * scale)),
            Image.LANCZOS,
        )

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    text = f"ART MARKETPLACE  •  {artist_name}"
    font_size = max(18, min(42, image.width // 28))
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    padding = max(12, image.width // 80)
    x = image.width - text_w - padding
    y = image.height - text_h - padding

    draw.rounded_rectangle(
        (x - padding, y - padding // 2, x + text_w + padding, y + text_h + padding // 2),
        radius=8,
        fill=(255, 255, 255, 115),
    )
    draw.text((x, y), text, font=font, fill=(25, 25, 25, 185))

    watermarked = Image.alpha_composite(image, overlay).convert("RGB")

    temp_path = None
    filename = f"watermark_{uuid4().hex}.jpg"
    try:
        fd, temp_path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        watermarked.save(temp_path, "JPEG", quality=88, optimize=True)
        return _upload_file_to_blob(
            temp_path,
            f"previews/{filename}",
            "image/jpeg",
        )
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def save_uploaded_artwork_image(upload, artist_name):
    """Save the original upload and generate its watermarked preview in Vercel Blob."""
    if not upload or not upload.filename:
        raise ValueError("Please choose an image file.")

    filename = secure_filename(upload.filename)
    if not filename or "." not in filename:
        raise ValueError("Invalid image file.")

    ext = filename.rsplit(".", 1)[1].lower()
    allowed = {"jpg", "jpeg", "png", "webp"}
    if ext not in allowed:
        raise ValueError("Please upload JPG, JPEG, PNG, or WEBP images only.")

    content_type = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }[ext]

    stored_name = f"original_{uuid4().hex}.{ext}"
    temp_path = None

    try:
        fd, temp_path = tempfile.mkstemp(suffix=f".{ext}")
        os.close(fd)
        upload.save(temp_path)

        with Image.open(temp_path) as image:
            image.load()
            preview_url = _build_watermarked_preview(image, artist_name)

        original_url = _upload_file_to_blob(
            temp_path,
            f"originals/{stored_name}",
            content_type,
        )
        return original_url, preview_url

    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The uploaded file is not a valid image or could not be stored.") from exc
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def create_watermarked_image(image_url, artist_name):
    """Download an artwork image, add a subtle watermark, and save the preview to Blob."""
    if not image_url.startswith(("http://", "https://")):
        raise ValueError("Image URL must start with http:// or https://.")

    req = urllib.request.Request(
        image_url,
        headers={"User-Agent": "ArtMarketplace/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read(4 * 1024 * 1024 + 1)

    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("Image is too large. Maximum size is 4 MB.")

    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:
        raise ValueError("The image URL does not contain a valid image.") from exc

    return _build_watermarked_preview(image, artist_name)


def get_artwork_image_data(form, upload, artist_name, existing=None, required=True):
    """Return (original_url, preview_url) using upload first, then URL, then existing image."""
    if upload and upload.filename:
        return save_uploaded_artwork_image(upload, artist_name)

    image_url = form.get("image_url", "").strip()
    if image_url.startswith(("http://", "https://")):
        return image_url, create_watermarked_image(image_url, artist_name)

    if image_url.startswith("/static/"):
        return image_url, image_url

    if existing and not required:
        return (
            existing.get("original_image_url") or existing.get("image_url"),
            existing.get("image_url"),
        )

    if required:
        raise ValueError("Please upload an image or enter an Image URL.")
    return None, None


def validate_artwork(form, image_required=True, default_artist=None):
    title = form.get("title", "").strip()
    artist = (default_artist or form.get("artist", "")).strip()
    category = form.get("category", "").strip()
    description = form.get("description", "").strip()
    image_url = form.get("image_url", "").strip()
    price_text = form.get("price", "").strip()
    stock_text = form.get("stock", "").strip()

    errors = []
    if len(title) < 2:
        errors.append("Artwork title must be at least 2 characters long.")
    if not artist:
        errors.append("Please enter the artist name.")
    if not category:
        errors.append("Please select a category.")
    if not description:
        errors.append("Please enter an artwork description.")
    if image_required and image_url and not image_url.startswith(("http://", "https://", "/static/")):
        errors.append("Image URL must start with http://, https://, or /static/.")
    try:
        price = float(price_text)
        if price < 0:
            errors.append("Price cannot be negative.")
    except ValueError:
        price = 0
        errors.append("Price must be a number.")
    try:
        stock = int(stock_text)
        if stock < 0:
            errors.append("Stock cannot be negative.")
    except ValueError:
        stock = 0
        errors.append("Stock must be a whole number.")

    return errors, (title, artist, category, price, description, image_url, stock)


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    sort = request.args.get("sort", "newest")
    page = max(int(request.args.get("page", 1)), 1)
    per_page = 6

    conditions = ["status='approved'", "stock > 0"]
    params = []
    if q:
        conditions.append("(title ILIKE %s OR artist ILIKE %s OR description ILIKE %s)")
        like = f"%{q}%"
        params += [like, like, like]
    if category:
        conditions.append("category=%s")
        params.append(category)

    order_by = {
        "newest": "created_at DESC",
        "price_low": "price ASC",
        "price_high": "price DESC",
        "title": "title ASC",
    }.get(sort, "created_at DESC")

    where = " AND ".join(conditions)
    total = query(f"SELECT COUNT(*) AS n FROM artworks WHERE {where}", params, True)["n"]
    offset = (page - 1) * per_page
    artworks = query(
        f"SELECT * FROM artworks WHERE {where} ORDER BY {order_by} LIMIT %s OFFSET %s",
        params + [per_page, offset],
    )
    categories = query("SELECT DISTINCT category FROM artworks ORDER BY category")
    pages = max((total + per_page - 1) // per_page, 1)
    return render_template("index.html", artworks=artworks, categories=categories, q=q, category=category, sort=sort, page=page, pages=pages)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        confirm = request.form.get("confirm", "")
        account_type = request.form.get("account_type", "customer").strip().lower()

        if account_type not in {"customer", "artist"}:
            flash("Invalid account type.", "danger")
            return render_template("register.html")

        if password != confirm:
            flash("Passwords do not match.", "danger")
            return render_template("register.html")

        if query("SELECT id FROM users WHERE username=%s", (username,), True):
            flash("Username is already in use.", "danger")
            return render_template("register.html")

        if query("SELECT id FROM users WHERE LOWER(email)=LOWER(%s)", (email,), True):
            flash("Email is already in use.", "danger")
            return render_template("register.html")

        artist_status = "pending" if account_type == "artist" else "none"
        row = execute(
            "INSERT INTO users(username,email,password_hash,full_name,role,artist_status) "
            "VALUES(%s,%s,%s,%s,'customer',%s) RETURNING id",
            (username, email, generate_password_hash(password), full_name, artist_status),
            fetchone=True,
        )

        session["user_id"] = row["id"]

        log_action(
            "REGISTER",
            "user",
            row["id"],
            f"username={username}, account_type={account_type}, artist_status={artist_status}",
        )

        if account_type == "artist":
            flash("Please complete your Artist application before admin review.", "success")
            return redirect(url_for("apply_artist"))

        flash("Account created successfully.", "success")
        return redirect(url_for("index"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username_or_email = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = query(
            "SELECT * FROM users WHERE username=%s OR LOWER(email)=LOWER(%s)",
            (username_or_email, username_or_email),
            True,
        )

        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid username/email or password.", "danger")
        else:
            session["user_id"] = user["id"]
            session.pop("staff_verified", None)
            session.pop("staff_access_attempts", None)
            log_action("LOGIN", "user", user["id"], "successful login")
            return redirect(request.args.get("next") or url_for("index"))

    return render_template("login.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()

        if email:
            user = query(
                "SELECT id FROM users WHERE LOWER(email)=LOWER(%s)",
                (email,),
                True,
            )

            if user:
                token = secrets.token_urlsafe(32)
                token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
                expires_at = datetime.utcnow() + timedelta(hours=1)

                execute(
                    "UPDATE password_reset_tokens SET used_at=CURRENT_TIMESTAMP "
                    "WHERE user_id=%s AND used_at IS NULL",
                    (user["id"],),
                )
                execute(
                    "INSERT INTO password_reset_tokens "
                    "(user_id,token_hash,expires_at) VALUES(%s,%s,%s)",
                    (user["id"], token_hash, expires_at),
                )

                reset_url = url_for("reset_password", token=token, _external=True)
                flash(f"Demo reset link: {reset_url}", "info")

        flash("If that email is registered, a password reset link has been generated.", "success")
        return redirect(url_for("forgot_password"))

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    reset = query(
        """SELECT id,user_id FROM password_reset_tokens
           WHERE token_hash=%s AND used_at IS NULL AND expires_at>CURRENT_TIMESTAMP""",
        (token_hash,),
        True,
    )

    if not reset:
        flash("This password reset link is invalid or has expired.", "danger")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if len(password) < 8:
            flash("Password must be at least 8 characters long.", "danger")
            return render_template("reset_password.html", token=token)

        if password != confirm:
            flash("Passwords do not match.", "danger")
            return render_template("reset_password.html", token=token)

        execute(
            "UPDATE users SET password_hash=%s WHERE id=%s",
            (generate_password_hash(password), reset["user_id"]),
        )
        execute(
            "UPDATE password_reset_tokens SET used_at=CURRENT_TIMESTAMP WHERE id=%s",
            (reset["id"],),
        )
        log_action("PASSWORD_RESET", "user", reset["user_id"], "password reset completed")
        flash("Password reset successfully. You can now log in.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)


@app.route("/logout")
def logout():
    if g.user:
        log_action("LOGOUT", "user", g.user["id"], "logout")
    session.clear()
    return redirect(url_for("index"))

@app.route("/artwork/<int:artwork_id>")
def artwork_detail(artwork_id):
    artwork = query("SELECT * FROM artworks WHERE id=%s", (artwork_id,), True)
    if not artwork:
        return "Not Found", 404
    if artwork["status"] != "approved" and (not g.user or g.user["role"] not in ("admin", "staff")):
        return "Not Found", 404
    reviews = query("SELECT r.*,u.username FROM reviews r JOIN users u ON u.id=r.user_id WHERE r.artwork_id=%s ORDER BY r.created_at DESC", (artwork_id,))
    liked = bool(g.user and query("SELECT 1 FROM likes WHERE user_id=%s AND artwork_id=%s", (g.user["id"], artwork_id), True))
    followed = bool(g.user and query("SELECT 1 FROM follows WHERE user_id=%s AND artist_name=%s", (g.user["id"], artwork["artist"]), True))
    return render_template("artwork_detail.html", artwork=artwork, reviews=reviews, liked=liked, followed=followed)


@app.route("/cart/add/<int:artwork_id>", methods=["POST"])
@login_required
def cart_add(artwork_id):
    artwork = query("SELECT id,title,stock FROM artworks WHERE id=%s AND status='approved'", (artwork_id,), True)
    if not artwork or artwork["stock"] < 1:
        flash("This artwork is out of stock or unavailable.", "danger")
        return redirect(url_for("index"))
    cart = session.setdefault("cart", {})
    key = str(artwork_id)
    cart[key] = min(int(cart.get(key, 0)) + 1, artwork["stock"])
    session.modified = True
    flash("Artwork added to cart.", "success")
    return redirect(url_for("cart"))


@app.route("/cart")
@login_required
def cart():
    cart_data = session.get("cart", {})
    items = []
    total = 0
    for artwork_id, quantity in cart_data.items():
        artwork = query("SELECT * FROM artworks WHERE id=%s AND status='approved'", (int(artwork_id),), True)
        if artwork:
            quantity = min(int(quantity), artwork["stock"])
            subtotal = float(artwork["price"]) * quantity
            total += subtotal
            items.append({"artwork": artwork, "quantity": quantity, "subtotal": subtotal})
    return render_template("cart.html", items=items, total=total)


@app.route("/cart/remove/<int:artwork_id>", methods=["POST"])
@login_required
def cart_remove(artwork_id):
    cart = session.get("cart", {})
    cart.pop(str(artwork_id), None)
    session["cart"] = cart
    return redirect(url_for("cart"))


@app.route("/checkout", methods=["POST"])
@login_required
def checkout():
    cart_data = session.get("cart", {})

    if not cart_data:
        flash("Your cart is empty.", "warning")
        return redirect(url_for("cart"))

    validated = []
    total = 0

    for artwork_id, quantity in cart_data.items():
        artwork = query(
            "SELECT * FROM artworks WHERE id=%s AND status='approved'",
            (int(artwork_id),),
            True
        )

        if not artwork or artwork["stock"] < int(quantity):
            flash("Some items in your cart are no longer available in the requested quantity.", "danger")
            return redirect(url_for("cart"))

        subtotal = float(artwork["price"]) * int(quantity)
        total += subtotal
        validated.append((artwork, int(quantity)))

    order = execute(
        "INSERT INTO orders(user_id,total,status,payment_method) "
        "VALUES(%s,%s,'pending_payment','promptpay') "
        "RETURNING id",
        (g.user["id"], total),
        fetchone=True,
    )

    for artwork, quantity in validated:
        execute(
            "INSERT INTO order_items"
            "(order_id,artwork_id,quantity,unit_price) "
            "VALUES(%s,%s,%s,%s)",
            (
                order["id"],
                artwork["id"],
                quantity,
                artwork["price"]
            ),
        )

        execute(
            "UPDATE artworks "
            "SET stock=stock-%s,updated_at=CURRENT_TIMESTAMP "
            "WHERE id=%s",
            (quantity, artwork["id"])
        )

    session["cart"] = {}

    log_action(
        "CREATE_ORDER",
        "order",
        order["id"],
        f"total={total:.2f}"
    )

    return redirect(url_for("payment", order_id=order["id"]))

@app.route("/payment/<int:order_id>")
@login_required
def payment(order_id):
    order = query("SELECT * FROM orders WHERE id=%s AND user_id=%s", (order_id, g.user["id"]), True)
    if not order:
        return "Not Found", 404
    return render_template("payment.html", order=order)


@app.route("/payment/<int:order_id>/confirm", methods=["POST"])
@login_required
def confirm_payment(order_id):
    order = query("SELECT * FROM orders WHERE id=%s AND user_id=%s", (order_id, g.user["id"]), True)
    if not order:
        return "Not Found", 404
    execute("UPDATE orders SET status='payment_review',updated_at=CURRENT_TIMESTAMP WHERE id=%s", (order_id,))
    log_action("PAYMENT_CONFIRM", "order", order_id, "customer submitted payment confirmation")
    flash("Payment confirmation submitted. Please wait for staff review.", "success")
    return redirect(url_for("my_orders"))


@app.route("/orders")
@login_required
def my_orders():
    orders = query("SELECT * FROM orders WHERE user_id=%s ORDER BY created_at DESC", (g.user["id"],))
    return render_template("orders.html", orders=orders, admin_view=False)


# -----------------------------------------------------------------------------
# Admin / Staff
# -----------------------------------------------------------------------------


@app.route("/artwork/<int:artwork_id>/like", methods=["POST"])
@login_required
def like_artwork(artwork_id):
    exists = query("SELECT 1 FROM likes WHERE user_id=%s AND artwork_id=%s", (g.user["id"], artwork_id), True)
    if exists:
        execute("DELETE FROM likes WHERE user_id=%s AND artwork_id=%s", (g.user["id"], artwork_id))
    else:
        execute("INSERT INTO likes(user_id,artwork_id) VALUES(%s,%s)", (g.user["id"], artwork_id))
    log_action("LIKE_TOGGLE", "artwork", artwork_id, "toggle like")
    return redirect(url_for("artwork_detail", artwork_id=artwork_id))

@app.route("/artist/<path:artist_name>/follow", methods=["POST"])
@login_required
def follow_artist(artist_name):
    exists = query("SELECT 1 FROM follows WHERE user_id=%s AND artist_name=%s", (g.user["id"], artist_name), True)
    if exists:
        execute("DELETE FROM follows WHERE user_id=%s AND artist_name=%s", (g.user["id"], artist_name))
    else:
        execute("INSERT INTO follows(user_id,artist_name) VALUES(%s,%s)", (g.user["id"], artist_name))
    log_action("FOLLOW_TOGGLE", "artist", None, artist_name)
    return redirect(request.referrer or url_for("index"))

@app.route("/artwork/<int:artwork_id>/review", methods=["POST"])
@login_required
def review_artwork(artwork_id):
    try:
        rating = int(request.form.get("rating", "0"))
    except ValueError:
        rating = 0
    comment = request.form.get("comment", "").strip()
    if rating not in range(1, 6) or not comment:
        flash("A review must include a rating from 1 to 5 and a comment.", "danger")
        return redirect(url_for("artwork_detail", artwork_id=artwork_id))
    execute("""INSERT INTO reviews(user_id,artwork_id,rating,comment) VALUES(%s,%s,%s,%s)
              ON CONFLICT(user_id,artwork_id) DO UPDATE SET rating=EXCLUDED.rating,comment=EXCLUDED.comment""",
            (g.user["id"], artwork_id, rating, comment))
    log_action("REVIEW", "artwork", artwork_id, f"rating={rating}")
    flash("Review saved.", "success")
    return redirect(url_for("artwork_detail", artwork_id=artwork_id))

@app.route("/commission", methods=["GET", "POST"])
@login_required
def commission_request():
    artists = query(
        "SELECT id, full_name, username FROM users "
        "WHERE role='artist' AND artist_status='approved' "
        "ORDER BY full_name"
    )

    if request.method == "POST":
        artist_name = request.form.get("artist_name", "").strip()
        artist = query(
            "SELECT id, full_name FROM users "
            "WHERE LOWER(full_name)=LOWER(%s) "
            "AND role='artist' AND artist_status='approved'",
            (artist_name,),
            True,
        )
        brief = request.form.get("brief", "").strip()

        try:
            budget = float(request.form.get("budget", "0"))
        except ValueError:
            budget = -1

        if not artist or not brief or budget < 0:
            flash("Please select an approved artist and enter a valid brief and budget.", "danger")
        else:
            row = execute(
                "INSERT INTO commission_requests(customer_id,artist_name,brief,budget) "
                "VALUES(%s,%s,%s,%s) RETURNING id",
                (g.user["id"], artist["full_name"], brief, budget),
                fetchone=True,
            )
            log_action("COMMISSION_REQUEST", "commission", row["id"], artist["full_name"])
            flash("Commission request submitted.", "success")
            return redirect(url_for("my_commissions"))

    return render_template("commission.html", artists=artists)


@app.route("/apply-artist", methods=["GET", "POST"])
@login_required
def apply_artist():
    if g.user["role"] in ("artist", "admin", "staff"):
        flash("Your current account already has artist/staff access.", "info")
        return redirect(url_for("artist_dashboard") if g.user["role"] == "artist" else url_for("index"))

    current_status = g.user.get("artist_status") or "none"
    has_application_details = bool((g.user.get("artist_bio") or "").strip()) or bool((g.user.get("portfolio_url") or "").strip())

    if request.method == "POST":
        bio = request.form.get("artist_bio", "").strip()
        portfolio_url = request.form.get("portfolio_url", "").strip()

        if len(bio) < 20:
            flash("Please tell us a little more about your work (at least 20 characters).", "danger")
            return render_template("apply_artist.html", pending=False, rejected=(current_status == "rejected"), user=g.user)

        if portfolio_url and not portfolio_url.startswith(("http://", "https://")):
            flash("Portfolio URL must start with http:// or https://.", "danger")
            return render_template("apply_artist.html", pending=False, rejected=(current_status == "rejected"), user=g.user)

        execute(
            "UPDATE users SET artist_status='pending', artist_bio=%s, portfolio_url=%s, "
            "artist_applied_at=CURRENT_TIMESTAMP WHERE id=%s",
            (bio, portfolio_url or None, g.user["id"]),
        )
        log_action("ARTIST_APPLY", "user", g.user["id"], "artist application submitted")
        flash("Artist application submitted. Please wait for admin approval.", "success")
        return redirect(url_for("index"))

    pending = current_status == "pending" and has_application_details
    rejected = current_status == "rejected"
    return render_template("apply_artist.html", pending=pending, rejected=rejected, user=g.user)


@app.route("/commissions")
@login_required
def my_commissions():
    commissions = query(
        """SELECT c.*, u.full_name AS artist_full_name
           FROM commission_requests c
           LEFT JOIN users u ON LOWER(u.full_name)=LOWER(c.artist_name)
           WHERE c.customer_id=%s
           ORDER BY c.created_at DESC""",
        (g.user["id"],),
    )
    return render_template("my_commissions.html", commissions=commissions)



@app.route("/artists")
def artist_directory():
    artists = query(
        """
        SELECT
            u.id,
            u.username,
            u.full_name,
            u.artist_bio,
            u.portfolio_url,
            COUNT(DISTINCT a.id) FILTER (WHERE a.status='approved') AS artwork_count,
            COUNT(DISTINCT f.user_id) AS follower_count
        FROM users u
        LEFT JOIN artworks a ON a.created_by = u.id
        LEFT JOIN follows f ON LOWER(f.artist_name) = LOWER(u.full_name)
        WHERE u.role='artist' AND u.artist_status='approved'
        GROUP BY u.id
        ORDER BY u.full_name ASC
        """
    )
    return render_template("artists.html", artists=artists)


@app.route("/artist/profile/<username>")
def artist_profile(username):
    artist = query(
        """
        SELECT id, username, full_name, artist_bio, portfolio_url
        FROM users
        WHERE LOWER(username)=LOWER(%s)
          AND role='artist'
          AND artist_status='approved'
        """,
        (username,),
        True,
    )

    if not artist:
        return "Not Found", 404

    artworks = query(
        """
        SELECT id,title,artist,category,price,description,image_url,tags,stock,created_at
        FROM artworks
        WHERE created_by=%s
          AND status='approved'
          AND stock > 0
        ORDER BY created_at DESC
        """,
        (artist["id"],),
    )

    follower_count = query(
        "SELECT COUNT(*) AS n FROM follows WHERE LOWER(artist_name)=LOWER(%s)",
        (artist["full_name"],),
        True,
    )["n"]

    followed = False
    if g.user:
        followed = bool(
            query(
                "SELECT 1 FROM follows WHERE user_id=%s AND LOWER(artist_name)=LOWER(%s)",
                (g.user["id"], artist["full_name"]),
                True,
            )
        )

    return render_template(
        "artist_profile.html",
        artist=artist,
        artworks=artworks,
        follower_count=follower_count,
        followed=followed,
        is_owner=bool(g.user and g.user["id"] == artist["id"]),
    )


@app.route("/artist/profile/edit", methods=["GET", "POST"])
@artist_required
def artist_profile_edit():
    if request.method == "POST":
        bio = request.form.get("artist_bio", "").strip()
        portfolio_url = request.form.get("portfolio_url", "").strip()

        if len(bio) < 20:
            flash("Please tell us a little more about your work (at least 20 characters).", "danger")
            return render_template("artist_profile_edit.html", artist=g.user)

        if portfolio_url and not portfolio_url.startswith(("http://", "https://")):
            flash("Portfolio URL must start with http:// or https://.", "danger")
            return render_template("artist_profile_edit.html", artist=g.user)

        execute(
            "UPDATE users SET artist_bio=%s, portfolio_url=%s WHERE id=%s",
            (bio, portfolio_url or None, g.user["id"]),
        )
        log_action("PROFILE_UPDATE", "user", g.user["id"], "artist profile updated")
        flash("Artist profile updated.", "success")
        return redirect(url_for("artist_profile", username=g.user["username"]))

    return render_template("artist_profile_edit.html", artist=g.user)

@app.route("/artist/dashboard")
@artist_required
def artist_dashboard():
    stats = {
        "artworks": query("SELECT COUNT(*) AS n FROM artworks WHERE created_by=%s", (g.user["id"],), True)["n"],
        "approved": query("SELECT COUNT(*) AS n FROM artworks WHERE created_by=%s AND status='approved'", (g.user["id"],), True)["n"],
        "pending": query("SELECT COUNT(*) AS n FROM artworks WHERE created_by=%s AND status='pending'", (g.user["id"],), True)["n"],
        "sales": query(
            """SELECT COALESCE(SUM(oi.quantity * oi.unit_price),0) AS n
               FROM order_items oi
               JOIN orders o ON o.id=oi.order_id
               JOIN artworks a ON a.id=oi.artwork_id
               WHERE a.created_by=%s AND o.status IN ('paid','shipping','completed')""",
            (g.user["id"],), True
        )["n"],
        "commissions": query(
            "SELECT COUNT(*) AS n FROM commission_requests WHERE LOWER(artist_name)=LOWER(%s)",
            (g.user["full_name"],), True
        )["n"],
    }
    recent_artworks = query(
        "SELECT id,title,category,price,status,stock,created_at FROM artworks WHERE created_by=%s ORDER BY created_at DESC LIMIT 8",
        (g.user["id"],),
    )
    recent_commissions = query(
        """SELECT c.*,u.username AS customer_username,u.full_name AS customer_name
           FROM commission_requests c
           JOIN users u ON u.id=c.customer_id
           WHERE LOWER(c.artist_name)=LOWER(%s)
           ORDER BY c.created_at DESC LIMIT 8""",
        (g.user["full_name"],),
    )
    return render_template("artist_dashboard.html", stats=stats, recent_artworks=recent_artworks, recent_commissions=recent_commissions)


@app.route("/artist/artworks")
@artist_required
def artist_artworks():
    artworks = query(
        "SELECT * FROM artworks WHERE created_by=%s ORDER BY created_at DESC",
        (g.user["id"],),
    )
    return render_template("artist_artworks.html", artworks=artworks)


@app.route("/artist/artworks/new", methods=["GET", "POST"])
@artist_required
def artist_artwork_new():
    if request.method == "POST":
        errors, data = validate_artwork(
            request.form,
            image_required=not bool(request.files.get("image_file") and request.files.get("image_file").filename),
            default_artist=g.user["full_name"],
        )
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            title, _artist, category, price, description, image_url, stock = data
            try:
                original_url, watermarked_url = get_artwork_image_data(
                    request.form, request.files.get("image_file"), g.user["full_name"], required=True
                )
            except Exception as exc:
                flash(str(exc), "danger")
                return render_template("artist_artwork_form.html", artwork=None)

            row = execute(
                """INSERT INTO artworks(title,artist,category,price,description,image_url,original_image_url,digital_file_url,tags,status,stock,created_by)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s) RETURNING id""",
                (title, g.user["full_name"], category, price, description, watermarked_url, original_url,
                 request.form.get("digital_file_url", "").strip() or None,
                 request.form.get("tags", "").strip(), stock, g.user["id"]),
                fetchone=True,
            )
            log_action("CREATE", "artwork", row["id"], title)
            flash("Artwork submitted for approval.", "success")
            return redirect(url_for("artist_artworks"))
    return render_template("artist_artwork_form.html", artwork=None)


@app.route("/artist/artworks/<int:artwork_id>/edit", methods=["GET", "POST"])
@artist_required
def artist_artwork_edit(artwork_id):
    artwork = query("SELECT * FROM artworks WHERE id=%s AND created_by=%s", (artwork_id, g.user["id"]), True)
    if not artwork:
        return "Not Found", 404

    if request.method == "POST":
        errors, data = validate_artwork(
            request.form,
            image_required=not bool(request.files.get("image_file") and request.files.get("image_file").filename),
            default_artist=g.user["full_name"],
        )
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            title, _artist, category, price, description, image_url, stock = data
            try:
                original_image_url, watermarked_url = get_artwork_image_data(
                    request.form, request.files.get("image_file"), g.user["full_name"], existing=artwork, required=False
                )
            except Exception as exc:
                flash(str(exc), "danger")
                return render_template("artist_artwork_form.html", artwork=artwork)

            execute(
                """UPDATE artworks SET title=%s,artist=%s,category=%s,price=%s,description=%s,
                   image_url=%s,original_image_url=%s,digital_file_url=%s,tags=%s,stock=%s,
                   status='pending',updated_at=CURRENT_TIMESTAMP
                   WHERE id=%s AND created_by=%s""",
                (title, g.user["full_name"], category, price, description, watermarked_url, original_image_url,
                 request.form.get("digital_file_url", "").strip() or None,
                 request.form.get("tags", "").strip(), stock, artwork_id, g.user["id"]),
            )
            log_action("UPDATE", "artwork", artwork_id, title)
            flash("Artwork updated and sent for re-approval.", "success")
            return redirect(url_for("artist_artworks"))
    return render_template("artist_artwork_form.html", artwork=artwork)


@app.route("/artist/artworks/<int:artwork_id>/archive", methods=["POST"])
@artist_required
def artist_artwork_archive(artwork_id):
    artwork = query("SELECT title FROM artworks WHERE id=%s AND created_by=%s", (artwork_id, g.user["id"]), True)
    if artwork:
        execute("UPDATE artworks SET status='archived',updated_at=CURRENT_TIMESTAMP WHERE id=%s AND created_by=%s", (artwork_id, g.user["id"]))
        log_action("ARCHIVE", "artwork", artwork_id, artwork["title"])
        flash("Artwork archived.", "success")
    return redirect(url_for("artist_artworks"))


@app.route("/artist/commissions")
@artist_required
def artist_commissions():
    commissions = query(
        """SELECT c.*,u.username AS customer_username,u.full_name AS customer_name
           FROM commission_requests c
           JOIN users u ON u.id=c.customer_id
           WHERE LOWER(c.artist_name)=LOWER(%s)
           ORDER BY c.created_at DESC""",
        (g.user["full_name"],),
    )
    return render_template("artist_commissions.html", commissions=commissions)


@app.route("/artist/commissions/<int:commission_id>/status", methods=["POST"])
@artist_required
def artist_commission_status(commission_id):
    status = request.form.get("status", "")
    allowed = {"requested", "reviewing", "accepted", "rejected", "completed"}
    if status not in allowed:
        flash("Invalid commission status.", "danger")
        return redirect(url_for("artist_commissions"))

    commission = query(
        "SELECT id FROM commission_requests WHERE id=%s AND LOWER(artist_name)=LOWER(%s)",
        (commission_id, g.user["full_name"]), True
    )
    if not commission:
        flash("Commission request not found.", "danger")
        return redirect(url_for("artist_commissions"))

    execute("UPDATE commission_requests SET status=%s WHERE id=%s", (status, commission_id))
    log_action("UPDATE_STATUS", "commission", commission_id, f"status={status}")
    flash("Commission status updated.", "success")
    return redirect(url_for("artist_commissions"))


@app.route("/admin/commissions")
@staff_access_required
def admin_commissions():
    commissions = query(
        """SELECT c.*,u.username AS customer_username,u.full_name AS customer_name
           FROM commission_requests c
           JOIN users u ON u.id=c.customer_id
           ORDER BY c.created_at DESC"""
    )
    return render_template("admin_commissions.html", commissions=commissions)


@app.route("/admin/commissions/<int:commission_id>/status", methods=["POST"])
@staff_access_required
def update_commission_status(commission_id):
    status = request.form.get("status", "")
    allowed = {"requested", "reviewing", "accepted", "rejected", "completed"}
    if status not in allowed:
        flash("Invalid commission status.", "danger")
        return redirect(url_for("admin_commissions"))

    commission = query(
        "SELECT id FROM commission_requests WHERE id=%s",
        (commission_id,),
        True,
    )
    if not commission:
        flash("Commission request not found.", "danger")
        return redirect(url_for("admin_commissions"))

    execute(
        "UPDATE commission_requests SET status=%s WHERE id=%s",
        (status, commission_id),
    )
    log_action("UPDATE_STATUS", "commission", commission_id, f"status={status}")
    flash("Commission status updated.", "success")
    return redirect(url_for("admin_commissions"))


@app.route("/dashboard")
@staff_access_required
def dashboard():
    stats = {
        "users": query("SELECT COUNT(*) AS n FROM users", fetchone=True)["n"],
        "artworks": query("SELECT COUNT(*) AS n FROM artworks", fetchone=True)["n"],
        "orders": query("SELECT COUNT(*) AS n FROM orders", fetchone=True)["n"],
        "sales": query("SELECT COALESCE(SUM(total),0) AS n FROM orders WHERE status IN ('paid','shipping','completed')", fetchone=True)["n"],
        "commissions": query("SELECT COUNT(*) AS n FROM commission_requests WHERE status NOT IN ('completed','rejected')", fetchone=True)["n"],
    }
    recent_orders = query("SELECT o.*,u.username FROM orders o JOIN users u ON u.id=o.user_id ORDER BY o.created_at DESC LIMIT 8")
    return render_template("dashboard.html", stats=stats, recent_orders=recent_orders)


@app.route("/admin/artworks")
@staff_access_required
def admin_artworks():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    sql = "SELECT * FROM artworks WHERE 1=1"
    params = []
    if q:
        sql += " AND (title ILIKE %s OR artist ILIKE %s)"
        like = f"%{q}%"
        params += [like, like]
    if status:
        sql += " AND status=%s"
        params.append(status)
    sql += " ORDER BY created_at DESC"
    artworks = query(sql, params)
    return render_template("admin_artworks.html", artworks=artworks, q=q, status=status)


@app.route("/admin/artworks/new", methods=["GET", "POST"])
@staff_access_required
def admin_artwork_new():
    if request.method == "POST":
        errors, data = validate_artwork(request.form, image_required=not bool(request.files.get("image_file") and request.files.get("image_file").filename))
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            title, artist, category, price, description, image_url, stock = data
            try:
                original_url, watermarked_url = get_artwork_image_data(
                    request.form, request.files.get("image_file"), artist, required=True
                )
            except Exception as exc:
                flash(str(exc), "danger")
                return render_template("artwork_form.html", artwork=None)

            row = execute(
                """INSERT INTO artworks(title,artist,category,price,description,image_url,original_image_url,digital_file_url,tags,status,stock,created_by)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s) RETURNING id""",
                (title, artist, category, price, description, watermarked_url, original_url, request.form.get("digital_file_url", "").strip() or None, request.form.get("tags", "").strip(), stock, g.user["id"]), fetchone=True,
            )
            log_action("CREATE", "artwork", row["id"], title)
            flash("Artwork added and submitted for approval.", "success")
            return redirect(url_for("admin_artworks"))
    return render_template("artwork_form.html", artwork=None)


@app.route("/admin/artworks/<int:artwork_id>/edit", methods=["GET", "POST"])
@staff_access_required
def admin_artwork_edit(artwork_id):
    artwork = query("SELECT * FROM artworks WHERE id=%s", (artwork_id,), True)
    if not artwork:
        return "Not Found", 404
    if request.method == "POST":
        errors, data = validate_artwork(request.form, image_required=not bool(request.files.get("image_file") and request.files.get("image_file").filename))
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            title, artist, category, price, description, image_url, stock = data
            try:
                original_image_url, watermarked_url = get_artwork_image_data(
                    request.form, request.files.get("image_file"), artist, existing=artwork, required=False
                )
            except Exception as exc:
                flash(str(exc), "danger")
                return render_template("artwork_form.html", artwork=artwork)

            execute(
                """UPDATE artworks SET title=%s,artist=%s,category=%s,price=%s,description=%s,
                   image_url=%s,original_image_url=%s,digital_file_url=%s,tags=%s,stock=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s""",
                (title, artist, category, price, description, watermarked_url, original_image_url, request.form.get("digital_file_url", "").strip() or None, request.form.get("tags", "").strip(), stock, artwork_id),
            )
            log_action("UPDATE", "artwork", artwork_id, title)
            flash("Artwork updated.", "success")
            return redirect(url_for("admin_artworks"))
    return render_template("artwork_form.html", artwork=artwork)


@app.route("/admin/artworks/<int:artwork_id>/delete", methods=["POST"])
@admin_access_required
def admin_artwork_delete(artwork_id):
    artwork = query("SELECT title FROM artworks WHERE id=%s", (artwork_id,), True)
    if artwork:
        execute(
    "UPDATE artworks "
    "SET status='archived', updated_at=CURRENT_TIMESTAMP "
    "WHERE id=%s",
    (artwork_id,)
    )
        log_action("DELETE", "artwork", artwork_id, artwork["title"])
        flash("Artwork archived.", "success")
    return redirect(url_for("admin_artworks"))


@app.route("/admin/artworks/<int:artwork_id>/approve", methods=["POST"])
@staff_access_required
def approve_artwork(artwork_id):
    execute("UPDATE artworks SET status='approved',updated_at=CURRENT_TIMESTAMP WHERE id=%s", (artwork_id,))
    log_action("APPROVE", "artwork", artwork_id, "approved for marketplace")
    flash("Artwork approved.", "success")
    return redirect(url_for("admin_artworks"))


@app.route("/admin/orders")
@staff_access_required
def admin_orders():
    orders = query("SELECT o.*,u.username,u.full_name FROM orders o JOIN users u ON u.id=o.user_id ORDER BY o.created_at DESC")
    return render_template("admin_orders.html", orders=orders)


@app.route("/admin/orders/<int:order_id>/status", methods=["POST"])
@staff_access_required
def update_order_status(order_id):
    status = request.form.get("status", "")

    allowed = {
        "pending_payment",
        "payment_review",
        "paid",
        "shipping",
        "completed",
        "cancelled"
    }

    if status not in allowed:
        flash("Invalid order status.", "danger")
        return redirect(url_for("admin_orders"))

    # ดึงสถานะเดิมของ order
    order = query(
        "SELECT * FROM orders WHERE id=%s",
        (order_id,),
        True
    )

    if not order:
        flash("Order not found.", "danger")
        return redirect(url_for("admin_orders"))

    # ถ้าเปลี่ยนเป็น cancelled
    # และ order เดิมยังไม่ใช่ cancelled
    # ให้คืน stock ของ artwork
    if status == "cancelled" and order["status"] != "cancelled":

        items = query(
            "SELECT artwork_id, quantity "
            "FROM order_items "
            "WHERE order_id=%s",
            (order_id,)
        )

        for item in items:
            execute(
                "UPDATE artworks "
                "SET stock = stock + %s, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE id=%s",
                (item["quantity"], item["artwork_id"])
            )

    # เปลี่ยนสถานะ order
    execute(
        "UPDATE orders "
        "SET status=%s, updated_at=CURRENT_TIMESTAMP "
        "WHERE id=%s",
        (status, order_id)
    )

    log_action(
        "UPDATE_STATUS",
        "order",
        order_id,
        f"status={status}"
    )

    flash("Order status updated.", "success")
    return redirect(url_for("admin_orders"))

@app.route("/admin/logs")
@admin_access_required
def admin_logs():
    logs = query(
        """SELECT l.*,u.username FROM logs l LEFT JOIN users u ON u.id=l.user_id
           ORDER BY l.created_at DESC LIMIT 200"""
    )
    return render_template("logs.html", logs=logs)


@app.route("/admin/users")
@admin_access_required
def admin_users():
    users = query(
        """SELECT id,username,email,full_name,role,artist_status,artist_bio,portfolio_url,artist_applied_at,created_at
           FROM users ORDER BY created_at DESC"""
    )
    return render_template("users.html", users=users)


@app.route("/admin/users/<int:user_id>/artist-status", methods=["POST"])
@admin_access_required
def update_artist_status(user_id):
    status = request.form.get("status", "")
    if status not in {"approved", "rejected"}:
        flash("Invalid artist application status.", "danger")
        return redirect(url_for("admin_users"))

    user = query(
        "SELECT id,username,full_name,role,artist_status,artist_bio,portfolio_url FROM users WHERE id=%s",
        (user_id,), True
    )
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("admin_users"))

    if status == "approved":
        execute(
            "UPDATE users SET role='artist', artist_status='approved' WHERE id=%s",
            (user_id,),
        )
        detail = f"artist application approved: {user['username']}"
    else:
        execute(
            "UPDATE users SET role='customer', artist_status='rejected' WHERE id=%s",
            (user_id,),
        )
        detail = f"artist application rejected: {user['username']}"

    log_action("ARTIST_APPLICATION", "user", user_id, detail)
    flash(f"Artist application {status}.", "success")
    return redirect(url_for("admin_users"))


@app.cli.command("init-db")
def init_db_command():
    init_db()
    print("Database initialized")


if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(debug=True)
