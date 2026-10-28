import type { Queryable } from "./pgvector.js";

export interface MemoryClient extends Queryable {
  release(): void;
}

export interface MemoryPool extends Queryable {
  connect(): Promise<MemoryClient>;
}

export const DEFAULT_MAX_HISTORY_MESSAGES = 10;

export type MessageRole = "user" | "assistant";

export interface ConversationMessage {
  role: MessageRole;
  content: string;
  created_at: Date | string;
}

export class ConversationMemory {
  readonly pool: MemoryPool;
  readonly max_history_messages: number;

  constructor(
    pool: MemoryPool,
    max_history_messages = DEFAULT_MAX_HISTORY_MESSAGES,
  ) {
    if (!Number.isSafeInteger(max_history_messages) || max_history_messages < 0) {
      throw new RangeError("max_history_messages must be a non-negative integer");
    }

    this.pool = pool;
    this.max_history_messages = max_history_messages;
  }

  async add_message(
    session_id: string,
    role: MessageRole,
    content: string,
  ): Promise<void> {
    if (role !== "user" && role !== "assistant") {
      throw new TypeError("role must be 'user' or 'assistant'");
    }

    const client = await this.pool.connect();

    try {
      await client.query("BEGIN");

      await client.query(
        `INSERT INTO conversations (session_id)
         VALUES ($1)
         ON CONFLICT (session_id)
         DO UPDATE SET updated_at = now()`,
        [session_id],
      );

      await client.query(
        `INSERT INTO conversation_messages (session_id, role, content)
         VALUES ($1, $2, $3)`,
        [session_id, role, content],
      );

      await client.query("COMMIT");
    } catch (error) {
      try {
        await client.query("ROLLBACK");
      } catch {
        // Preserve the original database error.
      }
      throw error;
    } finally {
      client.release();
    }
  }

  async get_history(
    session_id: string,
    limit = this.max_history_messages,
  ): Promise<ConversationMessage[]> {
    if (!Number.isSafeInteger(limit) || limit < 0) {
      throw new RangeError("limit must be a non-negative integer");
    }
    if (limit === 0) return [];

    const result = await this.pool.query(
      `SELECT role, content, created_at
       FROM conversation_messages
       WHERE session_id = $1
       ORDER BY created_at DESC
       LIMIT $2`,
      [session_id, limit],
    );

    return result.rows.reverse().map((row) => ({
      role: row.role as MessageRole,
      content: String(row.content),
      created_at: row.created_at as Date | string,
    }));
  }

  async clear_session(session_id: string): Promise<void> {
    await this.pool.query(
      "DELETE FROM conversations WHERE session_id = $1",
      [session_id],
    );
  }

  async build_history_prompt(session_id: string): Promise<string> {
    const history = await this.get_history(session_id);
    if (history.length === 0) return "";

    const lines = history.map(
      (message) =>
        `${message.role.charAt(0).toUpperCase()}${message.role.slice(1)}: ${message.content}`,
    );

    return `Previous conversation:\n${lines.join("\n")}\n\n`;
  }
}
