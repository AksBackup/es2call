import sqlite3

DB_PATH = "callcenter.db"
EMAIL = "el@gmail.com"

conn = sqlite3.connect(DB_PATH)

try:
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE users
        SET email_verified = 1
        WHERE email = ?
        """,
        (EMAIL,)
    )

    conn.commit()

    if cursor.rowcount > 0:
        print(f"Email verified successfully: {EMAIL}")
    else:
        print(f"No user found with email: {EMAIL}")

finally:
    conn.close()