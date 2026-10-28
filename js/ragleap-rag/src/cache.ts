export interface CacheStats {
  hits: number;
  misses: number;
  hit_rate: number;
  size: number;
}

export class QueryEmbeddingCache {
  readonly max_size: number;
  private readonly store = new Map<string, number[]>();
  hits = 0;
  misses = 0;

  constructor(max_size = 1000) {
    this.max_size = max_size;
  }

  private key(query: string, model: string): string {
    return `${model}:${query}`;
  }

  get(query: string, model: string): number[] | null {
    const key = this.key(query, model);
    const embedding = this.store.get(key);

    if (embedding !== undefined) {
      // Refresh insertion order to mark this entry as recently used.
      this.store.delete(key);
      this.store.set(key, embedding);
      this.hits += 1;
      return embedding;
    }

    this.misses += 1;
    return null;
  }

  set(query: string, model: string, embedding: number[]): void {
    const key = this.key(query, model);

    this.store.delete(key);
    this.store.set(key, embedding);

    if (this.store.size > this.max_size) {
      const leastRecentlyUsedKey = this.store.keys().next().value;
      if (leastRecentlyUsedKey !== undefined) {
        this.store.delete(leastRecentlyUsedKey);
      }
    }
  }

  clear(): void {
    this.store.clear();
    this.hits = 0;
    this.misses = 0;
  }

  stats(): CacheStats {
    const total = this.hits + this.misses;

    return {
      hits: this.hits,
      misses: this.misses,
      hit_rate: total > 0
        ? Math.round((this.hits / total) * 10000) / 10000
        : 0,
      size: this.store.size,
    };
  }
}
