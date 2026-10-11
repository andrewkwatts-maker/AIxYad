import sqlite3
import sys

def inspect_db(db_path):
    """
    Reads the .aixyad/project.db database and prints task counts by status
    and the last 5 sprint summaries.
    """
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Task counts by status
        cursor.execute("SELECT COUNT(*) FROM issues WHERE status = 'backlog'")
        backlog_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM issues WHERE status = 'in_progress'")
        in_progress_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM issues WHERE status = 'done'")
        done_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM issues WHERE status = 'failed'")
        failed_count = cursor.fetchone()[0]

        print("Task Counts by Status:")
        print(f"  Backlog: {backlog_count}")
        print(f"  In Progress: {in_progress_count}")
        print(f"  Done: {done_count}")
        print(f"  Failed: {failed_count}")
        print("-" * 20)

        # Last 5 sprint summaries
        cursor.execute("SELECT sprint_number, status, completed, total FROM sprints ORDER BY sprint_number DESC LIMIT 5")
        sprint_summaries = cursor.fetchall()

        print("Last 5 Sprint Summaries:")
        for sprint_number, status, completed, total in sprint_summaries:
            print(f"  Sprint {sprint_number}: {status}, Completed: {completed}, Total: {total}")

    except sqlite3.Error as e:
        print(f"An error occurred: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python db_inspect.py <database_path>")
        sys.exit(1)

    db_path = sys.argv[1]
    inspect_db(db_path)
