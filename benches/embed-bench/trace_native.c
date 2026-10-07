/* Linux-only offline benchmark observer. No production linkage.
 * cc -shared -fPIC -O2 -Wall -Wextra -o .local/trace/libtrace_native.so \
 *   benches/embed-bench/trace_native.c -ldl
 * Activated explicitly by phase_trace.py only during the indexing job.
 * Binary records: <QQQQIIIIqq (64 bytes); all clocks are nanoseconds.
 * SQL = actual C API interval, before Python reacquires its GIL.
 * File IO includes only index.db / index.db-wal / index.db-journal.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>
#include <stdatomic.h>
#include <pthread.h>

struct event {
    uint64_t start, end, cpu_start, cpu_end;
    uint32_t tid, op, role, fd;
    int64_t size, result;
};
_Static_assert(sizeof(struct event) == 64, "unexpected trace layout");
static _Atomic int active;
static int output = -1;
static void *sqlite_lib;
static __thread int sql_depth;
static _Atomic unsigned char fd_roles[4096];
struct buffer {
    struct event data[512];
    size_t used;
    pthread_mutex_t lock;
    struct buffer *next;
};
static struct buffer *buffers;
static pthread_mutex_t buffers_lock = PTHREAD_MUTEX_INITIALIZER;
static __thread struct buffer *local_buffer;
static void flush_buffer(struct buffer *b) {
    size_t size = b->used * sizeof(struct event);
    if (size && syscall(SYS_write, output, b->data, size) != (ssize_t)size) _exit(120);
    b->used = 0;
}
static void append_event(const struct event *e) {
    if (!local_buffer) {
        local_buffer = calloc(1, sizeof(struct buffer));
        if (!local_buffer) _exit(123);
        pthread_mutex_init(&local_buffer->lock, NULL);
        pthread_mutex_lock(&buffers_lock);
        local_buffer->next = buffers;
        buffers = local_buffer;
        pthread_mutex_unlock(&buffers_lock);
    }
    pthread_mutex_lock(&local_buffer->lock);
    local_buffer->data[local_buffer->used++] = *e;
    if (local_buffer->used == 512) flush_buffer(local_buffer);
    pthread_mutex_unlock(&local_buffer->lock);
}
static uint64_t clock_ns(clockid_t id) {
    struct timespec t; clock_gettime(id, &t);
    return (uint64_t)t.tv_sec * 1000000000ULL + t.tv_nsec;
}
void nova_trace_enable(const char *path) {
    if (output < 0) output = (int)syscall(SYS_openat, AT_FDCWD, path,
                                         O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC, 0600);
    atomic_store(&active, output >= 0);
}
void nova_trace_disable(void) {
    atomic_store(&active, 0);
    pthread_mutex_lock(&buffers_lock);
    for (struct buffer *b = buffers; b; b = b->next) {
        pthread_mutex_lock(&b->lock);
        flush_buffer(b);
        pthread_mutex_unlock(&b->lock);
    }
    pthread_mutex_unlock(&buffers_lock);
}
int nova_trace_fd(void) { return output; }
static int path_role(const char *path) {
    if (!path) return 0;
    const char *base = strrchr(path, '/'); base = base ? base + 1 : path;
    if (!strcmp(base, "index.db-wal")) return 2;
    if (!strcmp(base, "index.db")) return 1;
    if (!strcmp(base, "index.db-journal")) return 3;
    if (strstr(base, "langchain-voyage")) return 4;
    return 0;
}
static int fd_role(int fd) {
    if (fd >= 0 && fd < 4096) {
        int cached = atomic_load(&fd_roles[fd]);
        if (cached) return cached;
    }
    char name[64], path[4096];
    snprintf(name, sizeof name, "/proc/self/fd/%d", fd);
    ssize_t n = readlink(name, path, sizeof(path)-1);
    if (n < 0) return 0;
    path[n] = 0;
    int role = path_role(path);
    if (fd >= 0 && fd < 4096 && role) atomic_store(&fd_roles[fd], role);
    return role;
}
int close(int fd) {
    static int (*original)(int);
    if (!original) original=dlsym(RTLD_NEXT,"close");
    /* Invalidate BEFORE the descriptor can be reused by another thread. */
    if (fd >= 0 && fd < 4096) atomic_store(&fd_roles[fd], 0);
    return original(fd);
}
static struct event begin_event(unsigned op, unsigned role, int fd, int64_t size) {
    struct event e = {0};
    e.tid = (uint32_t)syscall(SYS_gettid); e.op = op; e.role = role;
    e.fd = (uint32_t)fd; e.size = size;
    e.start = clock_ns(CLOCK_MONOTONIC); e.cpu_start = clock_ns(CLOCK_THREAD_CPUTIME_ID);
    return e;
}
static void end_event(struct event *e, int64_t result) {
    int saved_errno = errno;
    e->cpu_end = clock_ns(CLOCK_THREAD_CPUTIME_ID); e->end = clock_ns(CLOCK_MONOTONIC);
    e->result = result;
    append_event(e);
    errno = saved_errno;
}
static void *sql_symbol(const char *name) {
    if (!sqlite_lib) sqlite_lib = dlopen("libsqlite3.so.0", RTLD_NOW|RTLD_NOLOAD);
    if (!sqlite_lib) _exit(121);
    void *symbol = dlsym(sqlite_lib, name);
    if (!symbol) _exit(122);
    return symbol;
}
static int stmt_role(void *stmt) {
    static void *(*db_handle)(void *);
    static const char *(*filename)(void *, const char *);
    if (!db_handle) db_handle = sql_symbol("sqlite3_db_handle");
    if (!filename) filename = sql_symbol("sqlite3_db_filename");
    return path_role(filename(db_handle(stmt), "main"));
}
#define SQL_STMT(FN, OP) \
int FN(void *stmt) { \
    static int (*original)(void *); if (!original) original=sql_symbol(#FN); \
    int role = atomic_load(&active) && !sql_depth && stmt ? stmt_role(stmt) : 0; \
    if (!role) return original(stmt); \
    struct event e=begin_event(OP, role, -1, 0); \
    ++sql_depth; int r=original(stmt); --sql_depth; end_event(&e,r); return r; \
}
SQL_STMT(sqlite3_step, 10)
SQL_STMT(sqlite3_reset, 11)
SQL_STMT(sqlite3_finalize, 12)

int sqlite3_prepare_v2(void *db, const char *sql, int n, void **stmt, const char **tail) {
    static int (*original)(void *, const char *, int, void **, const char **);
    static const char *(*filename)(void *, const char *);
    if (!original) original=sql_symbol("sqlite3_prepare_v2");
    if (!filename) filename=sql_symbol("sqlite3_db_filename");
    int role=atomic_load(&active) && !sql_depth ? path_role(filename(db,"main")) : 0;
    if (!role) return original(db,sql,n,stmt,tail);
    struct event e=begin_event(13,role,-1,0);
    ++sql_depth; int r=original(db,sql,n,stmt,tail); --sql_depth;
    end_event(&e,r); return r;
}
#define SYNC(FN, OP) \
int FN(int fd) { \
    static int (*original)(int); if (!original) original=dlsym(RTLD_NEXT,#FN); \
    int role=atomic_load(&active) ? fd_role(fd) : 0; \
    if (!role) return original(fd); \
    struct event e=begin_event(OP,role,fd,0); \
    int r=original(fd); end_event(&e,r); return r; \
}
SYNC(fsync, 1)
SYNC(fdatasync, 2)
#define PWRITE(FN, TYPE, OP) \
ssize_t FN(int fd, const void *buf, size_t n, TYPE offset) { \
    static ssize_t (*original)(int,const void *,size_t,TYPE); \
    if (!original) original=dlsym(RTLD_NEXT,#FN); \
    int role=atomic_load(&active) ? fd_role(fd) : 0; \
    if (!role) return original(fd,buf,n,offset); \
    struct event e=begin_event(OP,role,fd,(int64_t)n); \
    ssize_t r=original(fd,buf,n,offset); end_event(&e,r); return r; \
}
PWRITE(pwrite, off_t, 3)
PWRITE(pwrite64, off64_t, 4)

#define PREAD(FN, TYPE, OP) \
ssize_t FN(int fd, void *buf, size_t n, TYPE offset) { \
    static ssize_t (*original)(int,void *,size_t,TYPE); \
    if (!original) original=dlsym(RTLD_NEXT,#FN); \
    int role=atomic_load(&active) ? fd_role(fd) : 0; \
    if (!role || role == 4) return original(fd,buf,n,offset); \
    struct event e=begin_event(OP,role,fd,(int64_t)n); \
    ssize_t r=original(fd,buf,n,offset); end_event(&e,r); return r; \
}
PREAD(pread, off_t, 5)
PREAD(pread64, off64_t, 6)

int sqlite3_wal_checkpoint_v2(void *db, const char *name, int mode, int *log, int *done) {
    static int (*original)(void *,const char *,int,int *,int *);
    if (!original) original=sql_symbol("sqlite3_wal_checkpoint_v2");
    if (!atomic_load(&active)) return original(db,name,mode,log,done);
    struct event e=begin_event(14,1,-1,0);
    int r=original(db,name,mode,log,done);
    end_event(&e,r); return r;
}
int sqlite3_wal_checkpoint(void *db, const char *name) {
    static int (*original)(void *,const char *);
    if (!original) original=sql_symbol("sqlite3_wal_checkpoint");
    if (!atomic_load(&active)) return original(db,name);
    struct event e=begin_event(14,1,-1,0);
    int r=original(db,name);
    end_event(&e,r); return r;
}
