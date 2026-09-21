"""
Flask backend for the student expense tracker.
Run: python app.py
Server starts on http://127.0.0.1:5000

Requires: flask, werkzeug (both come with a plain `pip install flask`)
"""

import io
import csv
import os
import re
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, session, send_from_directory, Response
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "replace-this-with-a-random-secret-key"  # needed for sessions

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "expenses.db")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------- SERVE FRONTEND ----------

@app.route("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")


def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  # lets us access columns by name
    return conn


def login_required(f):
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Not logged in"}), 401
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper


# ---------- AUTH ----------

@app.route("/register", methods=["POST"])
def register():
    data = request.get_json()
    name = data.get("name")
    email = data.get("email")
    password = data.get("password")
    allowance_amount = data.get("allowance_amount", 0)
    allowance_date = data.get("allowance_date")

    if not name or not email or not password:
        return jsonify({"error": "name, email, and password are required"}), 400

    if not EMAIL_RE.match(email):
        return jsonify({"error": "Enter a valid email address"}), 400

    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    if allowance_date:
        try:
            parsed_date = datetime.strptime(allowance_date, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "allowance_date must be in YYYY-MM-DD format"}), 400
        if parsed_date < datetime.now().date():
            return jsonify({"error": "Allowance date cannot be in the past"}), 400

    password_hash = generate_password_hash(password)

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (name, email, password_hash, allowance_amount, allowance_date) VALUES (?, ?, ?, ?, ?)",
            (name, email, password_hash, allowance_amount, allowance_date),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "Email already registered"}), 409
    finally:
        conn.close()

    return jsonify({"message": "Registered successfully"}), 201


@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()

    if user is None or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Invalid email or password"}), 401

    session["user_id"] = user["id"]
    return jsonify({"message": "Logged in", "name": user["name"]}), 200


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    return jsonify({"message": "Logged out"}), 200


# ---------- EXPENSES ----------

@app.route("/expenses", methods=["POST"])
@login_required
def add_expense():
    data = request.get_json()
    amount = data.get("amount")
    description = data.get("description", "")
    category = data.get("category", "")

    if amount is None:
        return jsonify({"error": "amount is required"}), 400

    conn = get_db()
    conn.execute(
        "INSERT INTO transactions (user_id, amount, description, category) VALUES (?, ?, ?, ?)",
        (session["user_id"], amount, description, category),
    )
    conn.commit()
    conn.close()

    return jsonify({"message": "Expense added"}), 201


@app.route("/expenses", methods=["GET"])
@login_required
def get_expenses():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, amount, description, category, date FROM transactions WHERE user_id = ? ORDER BY date DESC",
        (session["user_id"],),
    ).fetchall()
    conn.close()

    expenses = [dict(row) for row in rows]
    return jsonify(expenses), 200


@app.route("/expenses/export", methods=["GET"])
@login_required
def export_expenses():
    month = request.args.get("month")  # expected format: YYYY-MM

    conn = get_db()
    if month:
        rows = conn.execute(
            """SELECT date, description, category, amount FROM transactions
               WHERE user_id = ? AND strftime('%Y-%m', date) = ?
               ORDER BY date""",
            (session["user_id"], month),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT date, description, category, amount FROM transactions WHERE user_id = ? ORDER BY date",
            (session["user_id"],),
        ).fetchall()

    user = conn.execute(
        "SELECT name, allowance_amount FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()
    conn.close()

    allowance_amount = user["allowance_amount"] if user else 0
    student_name = user["name"] if user else ""

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Expense statement for", student_name])
    writer.writerow(["Period", month if month else "All time"])
    writer.writerow([])
    writer.writerow(["Date", "Description", "Category", "Amount (NGN)"])

    total = 0
    for row in rows:
        writer.writerow([row["date"], row["description"], row["category"], row["amount"]])
        total += row["amount"] or 0

    writer.writerow([])
    writer.writerow(["Total expenses", "", "", total])
    writer.writerow(["Allowance", "", "", allowance_amount])
    writer.writerow(["Remaining", "", "", allowance_amount - total])

    filename = f"expense_statement_{month if month else 'all_time'}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------- ALLOWANCE COUNTDOWN ----------

@app.route("/allowance/countdown", methods=["GET"])
@login_required
def allowance_countdown():
    conn = get_db()
    user = conn.execute(
        "SELECT allowance_amount, allowance_date FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()
    conn.close()

    if user is None or not user["allowance_date"]:
        return jsonify({"error": "No allowance date set"}), 400

    try:
        target_date = datetime.strptime(user["allowance_date"], "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "allowance_date must be in YYYY-MM-DD format"}), 400

    days_left = (target_date - datetime.now()).days

    return jsonify({
        "allowance_amount": user["allowance_amount"],
        "allowance_date": user["allowance_date"],
        "days_left": max(days_left, 0),
    }), 200


@app.route("/allowance", methods=["PUT"])
@login_required
def update_allowance():
    data = request.get_json()
    allowance_amount = data.get("allowance_amount")
    allowance_date = data.get("allowance_date")

    if allowance_amount is None and not allowance_date:
        return jsonify({"error": "Provide a new amount or date to update"}), 400

    if allowance_date:
        try:
            parsed_date = datetime.strptime(allowance_date, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "allowance_date must be in YYYY-MM-DD format"}), 400
        if parsed_date < datetime.now().date():
            return jsonify({"error": "Allowance date cannot be in the past"}), 400

    conn = get_db()
    if allowance_amount is not None and allowance_date:
        conn.execute(
            "UPDATE users SET allowance_amount = ?, allowance_date = ? WHERE id = ?",
            (allowance_amount, allowance_date, session["user_id"]),
        )
    elif allowance_amount is not None:
        conn.execute(
            "UPDATE users SET allowance_amount = ? WHERE id = ?",
            (allowance_amount, session["user_id"]),
        )
    else:
        conn.execute(
            "UPDATE users SET allowance_date = ? WHERE id = ?",
            (allowance_date, session["user_id"]),
        )
    conn.commit()
    conn.close()

    return jsonify({"message": "Allowance updated"}), 200


# ---------- MONTHLY SUMMARY ----------

@app.route("/summary", methods=["GET"])
@login_required
def get_summary():
    conn = get_db()
    user = conn.execute(
        "SELECT allowance_amount FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()

    all_time_expenses = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM transactions WHERE user_id = ?",
        (session["user_id"],),
    ).fetchone()["total"]

    expenses_this_month = conn.execute(
        """SELECT COALESCE(SUM(amount), 0) AS total FROM transactions
           WHERE user_id = ? AND strftime('%Y-%m', date) = strftime('%Y-%m', 'now')""",
        (session["user_id"],),
    ).fetchone()["total"]

    today_spending = conn.execute(
        """SELECT COALESCE(SUM(amount), 0) AS total FROM transactions
           WHERE user_id = ? AND date(date) = date('now')""",
        (session["user_id"],),
    ).fetchone()["total"]

    conn.close()

    allowance_amount = user["allowance_amount"] if user else 0
    balance = allowance_amount - all_time_expenses
    remaining_budget = allowance_amount - expenses_this_month
    percent_used = round((expenses_this_month / allowance_amount) * 100, 1) if allowance_amount > 0 else 0

    return jsonify({
        "balance": round(balance, 2),
        "income_this_month": allowance_amount,
        "expenses_this_month": round(expenses_this_month, 2),
        "today_spending": round(today_spending, 2),
        "remaining_budget": round(remaining_budget, 2),
        "percent_used": percent_used,
    }), 200


# ---------- SAVINGS GOALS ----------

@app.route("/savings", methods=["POST"])
@login_required
def create_savings_goal():
    data = request.get_json()
    title = data.get("title", "My goal")
    target_amount = data.get("target_amount")

    if target_amount is None:
        return jsonify({"error": "target_amount is required"}), 400

    conn = get_db()
    conn.execute(
        "INSERT INTO savings_goals (user_id, title, target_amount) VALUES (?, ?, ?)",
        (session["user_id"], title, target_amount),
    )
    conn.commit()
    conn.close()

    return jsonify({"message": "Savings goal created"}), 201


@app.route("/savings", methods=["GET"])
@login_required
def get_savings_goals():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, target_amount, saved_amount FROM savings_goals WHERE user_id = ?",
        (session["user_id"],),
    ).fetchall()
    conn.close()

    goals = [dict(row) for row in rows]
    return jsonify(goals), 200


@app.route("/savings/<int:goal_id>/add", methods=["POST"])
@login_required
def add_to_savings(goal_id):
    data = request.get_json()
    amount = data.get("amount")

    if amount is None:
        return jsonify({"error": "amount is required"}), 400

    conn = get_db()
    conn.execute(
        "UPDATE savings_goals SET saved_amount = saved_amount + ? WHERE id = ? AND user_id = ?",
        (amount, goal_id, session["user_id"]),
    )
    conn.commit()
    conn.close()

    return jsonify({"message": "Savings updated"}), 200


if __name__ == "__main__":
    app.run(debug=True)