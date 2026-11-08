// Bot state in Cloudflare D1 (SQLite). See migrations/0001_init.sql.

export class Store {
  constructor(db) {
    this.db = db
  }

  async getState(key) {
    const row = await this.db.prepare('SELECT value FROM state WHERE key = ?').bind(key).first()
    return row ? row.value : null
  }

  async setState(key, value) {
    await this.db
      .prepare('INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value')
      .bind(key, String(value))
      .run()
  }

  async getReplied(videoUri) {
    return this.db.prepare('SELECT * FROM replied WHERE video_uri = ?').bind(videoUri).first()
  }

  // Insert or update one handled video. `bumpAttempts` counts a failed try.
  async putReplied(c, { status, error = null, replyUri = null, bumpAttempts = false, now }) {
    await this.db
      .prepare(
        `INSERT INTO replied (video_uri, video_cid, target_uri, target_cid, root_uri, root_cid,
                              author_did, status, attempts, last_error, reply_uri, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(video_uri) DO UPDATE SET
           target_uri = excluded.target_uri, target_cid = excluded.target_cid,
           root_uri = excluded.root_uri, root_cid = excluded.root_cid,
           status = excluded.status,
           attempts = replied.attempts + ?,
           last_error = excluded.last_error, reply_uri = excluded.reply_uri,
           updated_at = excluded.updated_at`,
      )
      .bind(
        c.video.uri, c.video.cid, c.target.uri, c.target.cid, c.root.uri, c.root.cid,
        c.author.did, status, bumpAttempts ? 1 : 0, error, replyUri, now,
        bumpAttempts ? 1 : 0,
      )
      .run()
  }

  // Failed videos still worth another try (oldest first).
  async retryable(maxAttempts, limit) {
    const { results } = await this.db
      .prepare("SELECT * FROM replied WHERE status = 'failed' AND attempts < ? ORDER BY updated_at LIMIT ?")
      .bind(maxAttempts, limit)
      .all()
    return results
  }

  async authorCount(did, day) {
    const row = await this.db.prepare('SELECT n FROM author_day WHERE author_did = ? AND day = ?').bind(did, day).first()
    return row ? row.n : 0
  }

  async bumpAuthor(did, day) {
    await this.db
      .prepare(
        'INSERT INTO author_day (author_did, day, n) VALUES (?, ?, 1) ON CONFLICT(author_did, day) DO UPDATE SET n = n + 1',
      )
      .bind(did, day)
      .run()
  }

  async pruneAuthorDays(beforeDay) {
    await this.db.prepare('DELETE FROM author_day WHERE day < ?').bind(beforeDay).run()
  }

  async getSession() {
    return this.db.prepare('SELECT * FROM session WHERE id = 1').first()
  }

  async putSession(s, now) {
    await this.db
      .prepare(
        `INSERT INTO session (id, did, handle, pds, access_jwt, refresh_jwt, updated_at)
         VALUES (1, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(id) DO UPDATE SET did = excluded.did, handle = excluded.handle, pds = excluded.pds,
           access_jwt = excluded.access_jwt, refresh_jwt = excluded.refresh_jwt, updated_at = excluded.updated_at`,
      )
      .bind(s.did, s.handle, s.pds, s.access_jwt, s.refresh_jwt, now)
      .run()
  }

  async clearSession() {
    await this.db.prepare('DELETE FROM session WHERE id = 1').run()
  }
}
