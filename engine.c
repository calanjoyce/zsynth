/*
 * zsynth-engine: real-time audio engine for ZSynth on the Cardputer Zero.
 *
 * Four tracks: 0 lead synth, 1 bass synth, 2 sampler, 3 drum kit.
 * A 16-step sequencer records what is played live (quantised to 16ths), and
 * notes can also be written one step at a time (stepset / stepdel / shift).
 *
 * Control: newline-separated text commands on stdin (see handle_cmd).
 * Status:  a small struct mmapped at /dev/shm/zsynth.state for the UI.
 *
 * Audio goes through PipeWire's PulseAudio interface (the ALSA default
 * device on this image is the HDMI port).
 *
 * Build: gcc -O2 -o zsynth-engine engine.c -lpulse-simple -lpulse -lm -lpthread
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <pulse/error.h>
#include <pulse/simple.h>
#include <math.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#define SR 48000
#define BLOCK 256
#define NTRACK 4
#define NSTEP 16
#define MAXEV 128
#define NVOICE 24
#define NPARAM 10
#define MAXSAMP (SR * 5)
#define SCOPE 640
#define NPAT 8
#define MAXCHAIN 64

enum { P_WAVE, P_CUT, P_RES, P_ATK, P_DEC, P_SUS, P_REL, P_ENV, P_DET, P_VOL };
enum { T_LEAD, T_BASS, T_SAMP, T_DRUM };
enum { D_KICK, D_RIM, D_SNARE, D_CLAP, D_CHAT, D_LTOM, D_OHAT, D_HTOM,
       D_BELL, D_CRASH };
static const int drum_of_semi[12] = {D_KICK, D_RIM, D_SNARE, D_CLAP, D_CHAT,
                                     D_LTOM, D_OHAT, D_HTOM, D_BELL, D_CRASH,
                                     D_OHAT, D_CLAP};

typedef struct {
    uint32_t magic, frame;
    int32_t playing, rec, step, bpm, sampling, sample_len;
    float in_level, out_level;
    int32_t voices, pad;
    float scope[SCOPE];
    uint8_t grid[NTRACK][NSTEP];
    uint8_t samp_ov[256];
    /* pattern banks and song chain (offset 2928) */
    int32_t cur_pat, next_pat, song_mode, chain_pos, chain_len, saved_seq;
    uint8_t chain[MAXCHAIN];
    uint8_t pgrid[NPAT][NTRACK][NSTEP];
    /* every note of the current pattern, for the step editor (offset 3528):
       per track a count, then (step, note, vel, len) per event */
    int32_t ev_n[NTRACK];
    uint8_t ev[NTRACK][MAXEV][4];
} Shm;

typedef struct {
    uint8_t step, note, vel, len, skip;
} Ev;

typedef struct {
    int active, track, note, seq, stage, drum;
    float vel, env, lp, bp, nlp, age;
    double ph, ph2, spos;
    long long off_at;
    unsigned seed;
} Voice;

static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static float P[NTRACK][NPARAM];
static Ev evs[NPAT][NTRACK][MAXEV];
static int nev[NPAT][NTRACK];
static int cur_pat, next_pat = -1, song_mode, chain_pos, chain_len, saved_seq;
static int chain[MAXCHAIN];
static int just_started;
static int pend[NTRACK][128]; /* index of event being recorded, -1 none */
static int pend_p[NTRACK][128];
static long long pend_t[NTRACK][128];
static Voice V[NVOICE];
static long long now_t;
static int playing, rec, step = 0, bpm = 110;
static double step_phase;
static float *sample;
static int sample_len;
static float click_env;
static float click_ph;
static Shm *shm;
static float scope_ring[2048];
static int scope_pos;
static volatile int running = 1;

/* sample recording */
static volatile int sampling;  /* request: keep capturing */
static volatile int capturing; /* capture thread busy, incl. saving */
static float *rec_buf;
static int rec_len;
static float in_level;
static char sample_path[512];
static pthread_t cap_thr;

static double sps(void) { return SR * 60.0 / bpm / 4.0; }

static float frand(unsigned *s) {
    *s = *s * 1664525u + 1013904223u;
    return (float)((int32_t)*s) / 2147483648.0f;
}

static float polyblep(double t, double dt) {
    if (t < dt) { t /= dt; return (float)(t + t - t * t - 1.0); }
    if (t > 1.0 - dt) { t = (t - 1.0) / dt; return (float)(t * t + t + t + 1.0); }
    return 0.0f;
}

static float osc(int wave, double t, double dt) {
    switch (wave) {
    case 0: return sinf((float)(2 * M_PI * t));
    case 1: return (float)(t < 0.5 ? 4 * t - 1 : 3 - 4 * t);
    case 2: return (float)(2 * t - 1) - polyblep(t, dt);
    default: {
        double t2 = fmod(t + 0.5, 1.0);
        return (t < 0.5 ? 1.0f : -1.0f) + polyblep(t, dt) - polyblep(t2, dt);
    }
    }
}

static float mtof(int n) { return 440.0f * powf(2.0f, (n - 69) / 12.0f); }

static void note_on(int tr, int note, int vel, int seq, long long off_at) {
    int best = -1;
    float oldest = -1;
    if (tr == T_SAMP && sample_len == 0) return;
    for (int i = 0; i < NVOICE; i++)
        if (!V[i].active) { best = i; break; }
    if (best < 0)
        for (int i = 0; i < NVOICE; i++)
            if (V[i].age > oldest) { oldest = V[i].age; best = i; }
    Voice *v = &V[best];
    memset(v, 0, sizeof *v);
    v->active = 1;
    v->track = tr;
    v->note = note;
    v->seq = seq;
    v->vel = vel / 127.0f;
    v->off_at = off_at;
    v->seed = (unsigned)(now_t * 2654435761u + note);
    v->drum = drum_of_semi[note % 12];
}

static void note_off(int tr, int note) {
    for (int i = 0; i < NVOICE; i++)
        if (V[i].active && !V[i].seq && V[i].track == tr && V[i].note == note
            && V[i].stage < 3)
            V[i].stage = 3;
}

/* Drum voice: returns sample, deactivates when done. */
static float drum(Voice *v) {
    float t = v->age / SR, dk = P[T_DRUM][P_DEC] > 0 ? P[T_DRUM][P_DEC] : 1;
    float n = frand(&v->seed), out = 0, e;
    v->nlp += 0.35f * (n - v->nlp);
    float hp = n - v->nlp;
    switch (v->drum) {
    case D_KICK: {
        float f = 48 + 140 * expf(-t / 0.035f);
        v->ph += f / SR;
        e = expf(-t / (0.28f * dk));
        out = sinf((float)(2 * M_PI * v->ph)) * e * 1.1f
              + n * expf(-t / 0.002f) * 0.3f;
        break;
    }
    case D_SNARE:
        v->ph += 185.0 / SR;
        e = expf(-t / (0.16f * dk));
        out = sinf((float)(2 * M_PI * v->ph)) * expf(-t / 0.06f) * 0.6f
              + hp * e * 0.8f;
        break;
    case D_CHAT: e = expf(-t / (0.035f * dk)); out = hp * e * 0.7f; break;
    case D_OHAT: e = expf(-t / (0.3f * dk)); out = hp * e * 0.55f; break;
    case D_CRASH: e = expf(-t / (0.9f * dk)); out = hp * e * 0.45f; break;
    case D_CLAP: {
        float b = 0;
        for (int k = 0; k < 3; k++) {
            float tk = t - k * 0.011f;
            if (tk >= 0) b += expf(-tk / 0.006f);
        }
        e = expf(-t / (0.18f * dk));
        v->bp += 0.25f * (hp - v->bp);
        out = v->bp * (b + e * 0.6f) * 1.4f;
        break;
    }
    case D_LTOM: case D_HTOM: {
        float f0 = v->drum == D_LTOM ? 95 : 150;
        v->ph += (f0 + f0 * 0.6f * expf(-t / 0.05f)) / SR;
        e = expf(-t / (0.32f * dk));
        out = sinf((float)(2 * M_PI * v->ph)) * e;
        break;
    }
    case D_RIM:
        v->ph += 1700.0 / SR;
        e = expf(-t / (0.02f * dk));
        out = (sinf((float)(2 * M_PI * v->ph)) * 0.6f + hp * 0.4f) * e;
        break;
    case D_BELL: {
        v->ph += 545.0 / SR;
        v->ph2 += 815.0 / SR;
        e = expf(-t / (0.14f * dk));
        float a = fmod(v->ph, 1.0) < 0.5 ? 1 : -1;
        float b = fmod(v->ph2, 1.0) < 0.5 ? 1 : -1;
        out = (a + b) * 0.25f * e;
        break;
    }
    default: e = 0;
    }
    if (t > 0.02f && e < 0.0005f) v->active = 0;
    return out * v->vel;
}

static float voice(Voice *v) {
    float *p = P[v->track];
    if (v->off_at >= 0 && now_t >= v->off_at && v->stage < 3) v->stage = 3;
    v->age += 1;
    if (v->track == T_DRUM) return drum(v);

    /* envelope */
    float atk = fmaxf(p[P_ATK], 0.001f), rel = fmaxf(p[P_REL], 0.005f);
    float sus = v->track == T_SAMP ? 1.0f : p[P_SUS];
    float dec = fmaxf(p[P_DEC], 0.005f);
    switch (v->stage) {
    case 0:
        v->env += 1.0f / (atk * SR);
        if (v->env >= 1) { v->env = 1; v->stage = 1; }
        break;
    case 1:
        v->env += (sus - v->env) * (1.0f - expf(-1.0f / (dec * SR * 0.3f)));
        break;
    case 3:
        v->env *= expf(-1.0f / (rel * SR * 0.3f));
        if (v->env < 0.0005f) { v->active = 0; return 0; }
        break;
    }

    float s;
    if (v->track == T_SAMP) {
        int i = (int)v->spos;
        if (i >= sample_len - 1) { v->active = 0; return 0; }
        float fr = (float)(v->spos - i);
        s = sample[i] * (1 - fr) + sample[i + 1] * fr;
        v->spos += powf(2.0f, (v->note - 60) / 12.0f);
        if (p[P_WAVE] < 0.5f && v->stage == 3) {
            /* one-shot: ignore note-off, play to the end of the sample */
            v->stage = 1;
        }
    } else {
        double f = mtof(v->note), dt = f / SR;
        int w = (int)p[P_WAVE];
        s = osc(w, v->ph, dt);
        v->ph += dt;
        if (v->ph >= 1) v->ph -= 1;
        if (p[P_DET] > 0.5f) {
            double dt2 = dt * pow(2.0, p[P_DET] / 1200.0);
            s = 0.6f * (s + osc(w, v->ph2, dt2));
            v->ph2 += dt2;
            if (v->ph2 >= 1) v->ph2 -= 1;
        }
    }

    /* state-variable low-pass, cutoff opened by the envelope */
    float fc = p[P_CUT];
    if (v->track != T_SAMP) fc *= powf(2.0f, p[P_ENV] * v->env * 4.0f);
    if (fc > SR / 6.5f) fc = SR / 6.5f;
    float f = 2.0f * sinf((float)M_PI * fc / SR);
    float q = 1.0f - p[P_RES] * 0.92f;
    v->lp += f * v->bp;
    float hp = s - v->lp - q * v->bp;
    v->bp += f * hp;
    return v->lp * v->env * v->vel;
}

static void switch_pattern(int p) {
    if (p == cur_pat) return;
    /* drop pending skips: that pattern's next pass starts fresh */
    for (int tr = 0; tr < NTRACK; tr++)
        for (int i = 0; i < nev[cur_pat][tr]; i++) evs[cur_pat][tr][i].skip = 0;
    cur_pat = p;
}

/* Called when the loop wraps to step 0: move on in the chain or to a
   queued pattern. */
static void loop_wrap(void) {
    if (just_started) { just_started = 0; return; }
    if (song_mode && chain_len > 0) {
        chain_pos = (chain_pos + 1) % chain_len;
        switch_pattern(chain[chain_pos]);
    } else if (next_pat >= 0) {
        switch_pattern(next_pat);
        next_pat = -1;
    }
}

static void seq_tick(void) {
    double len = sps();
    if (step == 0) loop_wrap();
    for (int tr = 0; tr < NTRACK; tr++)
        for (int i = 0; i < nev[cur_pat][tr]; i++) {
            Ev *e = &evs[cur_pat][tr][i];
            if (e->step != step) continue;
            if (e->skip) { e->skip = 0; continue; }
            note_on(tr, e->note, e->vel, 1,
                    now_t + (long long)(e->len * len * 0.92));
        }
    if (rec && step % 4 == 0) { click_env = step == 0 ? 0.5f : 0.25f; click_ph = 0; }
}

static void render(int16_t *out, int n) {
    float peak = 0;
    for (int k = 0; k < n; k++) {
        if (playing) {
            step_phase += 1;
            if (step_phase >= sps()) {
                step_phase -= sps();
                step = (step + 1) % NSTEP;
                seq_tick();
            }
        }
        float mix = 0;
        for (int i = 0; i < NVOICE; i++)
            if (V[i].active) mix += voice(&V[i]) * P[V[i].track][P_VOL];
        if (click_env > 0.0005f) {
            click_ph += 1600.0f / SR;
            mix += sinf(2 * (float)M_PI * click_ph) * click_env;
            click_env *= 0.9985f;
        }
        mix = tanhf(mix * 0.7f);
        scope_ring[scope_pos] = mix;
        scope_pos = (scope_pos + 1) & 2047;
        if (fabsf(mix) > peak) peak = fabsf(mix);
        int16_t s = (int16_t)(mix * 30000);
        out[2 * k] = out[2 * k + 1] = s;
        now_t++;
    }
    shm->out_level = peak;
}

static void publish(void) {
    int a = 0;
    for (int i = 0; i < NVOICE; i++) a += V[i].active;
    shm->voices = a;
    shm->playing = playing;
    shm->rec = rec;
    shm->step = step;
    shm->bpm = bpm;
    shm->sampling = capturing;
    shm->sample_len = sample_len;
    shm->in_level = in_level;
    for (int i = 0; i < SCOPE; i++)
        shm->scope[i] = scope_ring[(scope_pos - SCOPE + i) & 2047];
    memset(shm->pgrid, 0, sizeof shm->pgrid);
    for (int p = 0; p < NPAT; p++)
        for (int tr = 0; tr < NTRACK; tr++)
            for (int i = 0; i < nev[p][tr]; i++) {
                uint8_t *c = &shm->pgrid[p][tr][evs[p][tr][i].step];
                if (*c < 255) (*c)++;
            }
    memcpy(shm->grid, shm->pgrid[cur_pat], sizeof shm->grid);
    for (int tr = 0; tr < NTRACK; tr++) {
        shm->ev_n[tr] = nev[cur_pat][tr];
        for (int i = 0; i < nev[cur_pat][tr]; i++) {
            Ev *e = &evs[cur_pat][tr][i];
            uint8_t *o = shm->ev[tr][i];
            o[0] = e->step; o[1] = e->note; o[2] = e->vel; o[3] = e->len;
        }
    }
    shm->cur_pat = cur_pat;
    shm->next_pat = next_pat;
    shm->song_mode = song_mode;
    shm->chain_pos = chain_pos;
    shm->chain_len = chain_len;
    shm->saved_seq = saved_seq;
    for (int i = 0; i < MAXCHAIN; i++) shm->chain[i] = (uint8_t)chain[i];
    shm->frame++;
}

static void overview(void) {
    memset(shm->samp_ov, 0, 256);
    if (!sample_len) return;
    for (int b = 0; b < 256; b++) {
        int s0 = (int)((long long)b * sample_len / 256);
        int s1 = (int)((long long)(b + 1) * sample_len / 256);
        float pk = 0;
        for (int i = s0; i < s1; i++) if (fabsf(sample[i]) > pk) pk = fabsf(sample[i]);
        shm->samp_ov[b] = (uint8_t)(fminf(pk, 1) * 255);
    }
}

/* ---------- WAV I/O ---------- */
static void wav_write(const char *path, const float *x, int n) {
    FILE *f = fopen(path, "wb");
    if (!f) return;
    uint32_t data = n * 2, riff = 36 + data, sr = SR, br = SR * 2, fmt = 16;
    uint16_t pcm = 1, ch = 1, ba = 2, bits = 16;
    fwrite("RIFF", 1, 4, f); fwrite(&riff, 4, 1, f); fwrite("WAVEfmt ", 1, 8, f);
    fwrite(&fmt, 4, 1, f); fwrite(&pcm, 2, 1, f); fwrite(&ch, 2, 1, f);
    fwrite(&sr, 4, 1, f); fwrite(&br, 4, 1, f); fwrite(&ba, 2, 1, f);
    fwrite(&bits, 2, 1, f); fwrite("data", 1, 4, f); fwrite(&data, 4, 1, f);
    for (int i = 0; i < n; i++) {
        int16_t s = (int16_t)(fmaxf(-1, fminf(1, x[i])) * 32767);
        fwrite(&s, 2, 1, f);
    }
    fclose(f);
}

/* Loads 16-bit PCM WAV (any rate, mono/stereo) as mono 48 kHz. */
static int wav_read(const char *path, float **outp) {
    FILE *f = fopen(path, "rb");
    if (!f) return -1;
    char id[4];
    uint32_t sz, rate = SR;
    uint16_t fmt = 0, ch = 1, bits = 16;
    int n = -1;
    float *o = NULL;
    if (fread(id, 1, 4, f) != 4 || memcmp(id, "RIFF", 4)) goto done;
    fseek(f, 12, SEEK_SET); /* skip "RIFF", size, "WAVE" */
    while (fread(id, 1, 4, f) == 4 && fread(&sz, 4, 1, f) == 1) {
        if (!memcmp(id, "fmt ", 4)) {
            uint8_t b[16];
            if (sz < 16 || fread(b, 1, 16, f) != 16) goto done;
            memcpy(&fmt, b, 2); memcpy(&ch, b + 2, 2); memcpy(&rate, b + 4, 4);
            memcpy(&bits, b + 14, 2);
            fseek(f, sz - 16 + (sz & 1), SEEK_CUR);
        } else if (!memcmp(id, "data", 4)) {
            if (fmt != 1 || bits != 16 || ch < 1 || !rate) goto done;
            int frames = sz / (2 * ch);
            int16_t *raw = malloc(sz);
            if (!raw) goto done;
            frames = fread(raw, 2 * ch, frames, f);
            double ratio = (double)rate / SR;
            int m = (int)(frames / ratio);
            if (m > MAXSAMP) m = MAXSAMP;
            o = malloc(sizeof(float) * (m + 1));
            for (int i = 0; i < m; i++) {
                double sp = i * ratio;
                int j = (int)sp;
                float fr = (float)(sp - j), a = 0, b2 = 0;
                for (int c = 0; c < ch; c++) {
                    a += raw[j * ch + c];
                    b2 += raw[(j + 1 < frames ? j + 1 : j) * ch + c];
                }
                o[i] = (a * (1 - fr) + b2 * fr) / (32768.0f * ch);
            }
            free(raw);
            n = m;
            break;
        } else {
            fseek(f, sz + (sz & 1), SEEK_CUR);
        }
    }
done:
    fclose(f);
    if (n <= 0) { free(o); return -1; }
    *outp = o;
    return n;
}

static void set_sample(float *buf, int n) {
    pthread_mutex_lock(&mu);
    for (int i = 0; i < NVOICE; i++)
        if (V[i].track == T_SAMP) V[i].active = 0;
    free(sample);
    sample = buf;
    sample_len = n;
    overview();
    pthread_mutex_unlock(&mu);
}

/* ---------- mic capture ---------- */
static void *capture(void *arg) {
    (void)arg;
    rec_len = 0;
    pa_sample_spec ss = {PA_SAMPLE_S16LE, SR, 1};
    pa_buffer_attr ba = {(uint32_t)-1, (uint32_t)-1, (uint32_t)-1,
                         (uint32_t)-1, 960};
    int err;
    pa_simple *pc = pa_simple_new(NULL, "ZSynth", PA_STREAM_RECORD, NULL,
                                  "sampler", &ss, NULL, &ba, &err);
    if (!pc) {
        fprintf(stderr, "engine: capture open failed: %s\n", pa_strerror(err));
        sampling = capturing = 0;
        return NULL;
    }
    int16_t buf[480];
    while (sampling && rec_len < MAXSAMP) {
        if (pa_simple_read(pc, buf, sizeof buf, &err) < 0) break;
        int r = 480;
        float pk = 0;
        for (int i = 0; i < r && rec_len < MAXSAMP; i++) {
            float s = buf[i] / 32768.0f;
            rec_buf[rec_len++] = s;
            if (fabsf(s) > pk) pk = fabsf(s);
        }
        in_level = pk;
    }
    pa_simple_free(pc);
    sampling = 0;
    in_level = 0;

    /* trim leading/trailing quiet, then normalise */
    float pk = 0;
    for (int i = 0; i < rec_len; i++) if (fabsf(rec_buf[i]) > pk) pk = fabsf(rec_buf[i]);
    if (pk < 0.0015f) {
        fprintf(stderr, "engine: sample silent\n");
        capturing = 0;
        return NULL;
    }
    float thr = pk * 0.06f;
    int a = 0, b = rec_len - 1;
    while (a < b && fabsf(rec_buf[a]) < thr) a++;
    while (b > a && fabsf(rec_buf[b]) < thr) b--;
    a = a > 240 ? a - 240 : 0;
    b = b + 2400 < rec_len ? b + 2400 : rec_len;
    int n = b - a;
    float *o = malloc(sizeof(float) * (n + 1));
    float g = 0.95f / pk;
    for (int i = 0; i < n; i++) {
        float fade = i < 96 ? i / 96.0f : (n - i < 480 ? (n - i) / 480.0f : 1);
        o[i] = rec_buf[a + i] * g * fade;
    }
    if (sample_path[0]) wav_write(sample_path, o, n);
    set_sample(o, n);
    capturing = 0;
    return NULL;
}

/* ---------- pattern save/load ---------- */
static void pat_save(const char *path) {
    FILE *f = fopen(path, "w");
    if (!f) return;
    fprintf(f, "bpm %d\nsongmode %d\nchain", bpm, song_mode);
    for (int i = 0; i < chain_len; i++) fprintf(f, " %d", chain[i]);
    fprintf(f, "\n");
    for (int p = 0; p < NPAT; p++)
        for (int tr = 0; tr < NTRACK; tr++)
            for (int i = 0; i < nev[p][tr]; i++) {
                Ev *e = &evs[p][tr][i];
                fprintf(f, "ev %d %d %d %d %d %d\n", p, tr, e->step, e->note,
                        e->vel, e->len);
            }
    fclose(f);
}

static void reset_song(void) {
    memset(nev, 0, sizeof nev);
    memset(pend, -1, sizeof pend);
    chain_len = chain_pos = song_mode = cur_pat = 0;
    next_pat = -1;
}

static void parse_chain(const char *s) {
    chain_len = 0;
    int v, n;
    while (chain_len < MAXCHAIN && sscanf(s, "%d%n", &v, &n) == 1) {
        if (v >= 0 && v < NPAT) chain[chain_len++] = v;
        s += n;
    }
    if (chain_pos >= chain_len) chain_pos = 0;
}

static void add_ev(int p, int tr, int st, int no, int ve, int le) {
    if (p < 0 || p >= NPAT || tr < 0 || tr >= NTRACK || nev[p][tr] >= MAXEV) return;
    Ev e = {(uint8_t)(st % NSTEP), (uint8_t)no, (uint8_t)ve, (uint8_t)le, 0};
    evs[p][tr][nev[p][tr]++] = e;
}

/* ---------- step editing ---------- */

/* Removes events at `st` on track `tr` of pattern `p` whose note is `no`
   (or every note if no < 0). Notes still being recorded on that track are
   let go so their indices can't point at the wrong event. */
static void del_ev(int p, int tr, int st, int no) {
    int k = 0;
    for (int i = 0; i < nev[p][tr]; i++) {
        Ev *e = &evs[p][tr][i];
        if (e->step == st && (no < 0 || e->note == no)) continue;
        evs[p][tr][k++] = *e;
    }
    if (k != nev[p][tr]) {
        nev[p][tr] = k;
        for (int n = 0; n < 128; n++)
            if (pend_p[tr][n] == p) pend[tr][n] = -1;
    }
}

/* Writes one note at a step, replacing the same note at that step. */
static void set_ev(int p, int tr, int st, int no, int ve, int le) {
    del_ev(p, tr, st, no);
    add_ev(p, tr, st, no, ve < 1 ? 1 : ve > 127 ? 127 : ve,
           le < 1 ? 1 : le > NSTEP ? NSTEP : le);
}

/* Moves a track's notes by d steps, wrapping round (tr < 0: all tracks). */
static void shift_ev(int p, int tr, int d) {
    for (int t = 0; t < NTRACK; t++) {
        if (tr >= 0 && t != tr) continue;
        for (int i = 0; i < nev[p][t]; i++) {
            Ev *e = &evs[p][t][i];
            e->step = (uint8_t)(((e->step + d) % NSTEP + NSTEP) % NSTEP);
            e->skip = 0;
        }
    }
}

/* Reads a song or pattern file. Lines it doesn't know are ignored, so the
   UI can keep its own settings in the same file. Old 5-field "ev" lines
   (from before pattern banks) go into pattern 1. */
static void pat_load(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return;
    char ln[512];
    reset_song();
    while (fgets(ln, sizeof ln, f)) {
        int p, tr, st, no, ve, le, b;
        if (sscanf(ln, "bpm %d", &b) == 1) { if (b >= 40 && b <= 300) bpm = b; }
        else if (sscanf(ln, "songmode %d", &b) == 1) song_mode = b != 0;
        else if (!strncmp(ln, "chain", 5)) parse_chain(ln + 5);
        else if (sscanf(ln, "ev %d %d %d %d %d %d", &p, &tr, &st, &no, &ve, &le) == 6)
            add_ev(p, tr, st, no, ve, le);
        else if (sscanf(ln, "ev %d %d %d %d %d", &tr, &st, &no, &ve, &le) == 5)
            add_ev(0, tr, st, no, ve, le);
    }
    fclose(f);
    if (song_mode && chain_len) cur_pat = chain[0];
}

/* Text after a command word, without the newline (paths may have spaces). */
static const char *arg(char *ln, const char *cmd) {
    size_t n = strlen(cmd);
    if (strncmp(ln, cmd, n) || ln[n] != ' ') return NULL;
    ln[strcspn(ln, "\r\n")] = 0;
    return ln + n + 1;
}

/* ---------- commands ---------- */
static void handle_cmd(char *ln) {
    int tr, a, b;
    float x;
    const char *path;
    Ev (*pat)[MAXEV] = evs[cur_pat];
    int *pn = nev[cur_pat];
    if (sscanf(ln, "on %d %d %d", &tr, &a, &b) == 3 && tr >= 0 && tr < NTRACK && a >= 0 && a < 128) {
        note_on(tr, a, b, 0, -1);
        if (playing && rec && pn[tr] < MAXEV) {
            double pos = step + step_phase / sps();
            int q = (int)floor(pos + 0.5);
            Ev e = {(uint8_t)(q % NSTEP), (uint8_t)a, (uint8_t)b, 1, (uint8_t)(q > step)};
            pat[tr][pn[tr]] = e;
            pend[tr][a] = pn[tr]++;
            pend_p[tr][a] = cur_pat;
            pend_t[tr][a] = now_t;
        }
    } else if (sscanf(ln, "off %d %d", &tr, &a) == 2 && tr >= 0 && tr < NTRACK && a >= 0 && a < 128) {
        note_off(tr, a);
        int i = pend[tr][a], p = pend_p[tr][a];
        if (i >= 0 && i < nev[p][tr]) {
            int l = (int)floor((now_t - pend_t[tr][a]) / sps() + 0.5);
            evs[p][tr][i].len = (uint8_t)(l < 1 ? 1 : l > 16 ? 16 : l);
        }
        pend[tr][a] = -1;
    } else if (sscanf(ln, "set %d %d %f", &tr, &a, &x) == 3 && tr >= 0 && tr < NTRACK && a >= 0 && a < NPARAM) {
        P[tr][a] = x;
    } else if (!strncmp(ln, "play", 4)) {
        playing = 1; step = NSTEP - 1; step_phase = sps() - 1;
        just_started = 1;
        if (song_mode && chain_len) { chain_pos = 0; switch_pattern(chain[0]); }
        else if (next_pat >= 0) { switch_pattern(next_pat); next_pat = -1; }
    } else if (!strncmp(ln, "stop", 4)) {
        playing = 0;
        for (int i = 0; i < NVOICE; i++) if (V[i].seq) V[i].stage = 3;
        if (next_pat >= 0) { switch_pattern(next_pat); next_pat = -1; }
    } else if (sscanf(ln, "rec %d", &a) == 1) {
        rec = a;
    } else if (sscanf(ln, "bpm %d", &a) == 1 && a >= 40 && a <= 300) {
        bpm = a;
    } else if (sscanf(ln, "pat %d", &a) == 1 && a >= 0 && a < NPAT) {
        if (playing && !song_mode) next_pat = a == cur_pat ? -1 : a;
        else if (!playing) switch_pattern(a);
    } else if (sscanf(ln, "songmode %d", &a) == 1) {
        song_mode = a != 0;
        if (song_mode && !playing && chain_len) { chain_pos = 0; switch_pattern(chain[0]); }
        if (song_mode) next_pat = -1;
    } else if (!strncmp(ln, "chain", 5) && (ln[5] == ' ' || ln[5] == '\n')) {
        parse_chain(ln + 5);
    } else if (sscanf(ln, "copy %d %d", &a, &b) == 2 && a >= 0 && a < NPAT && b >= 0 && b < NPAT) {
        if (a != b) { memcpy(evs[b], evs[a], sizeof evs[a]); memcpy(nev[b], nev[a], sizeof nev[a]); }
    } else if (sscanf(ln, "clearpat %d", &a) == 1 && a >= 0 && a < NPAT) {
        memset(nev[a], 0, sizeof nev[a]);
    } else if (!strncmp(ln, "stepset ", 8)) {
        int p, st, no, ve, le;
        if (sscanf(ln + 8, "%d %d %d %d %d %d", &p, &tr, &st, &no, &ve, &le) == 6
            && p >= 0 && p < NPAT && tr >= 0 && tr < NTRACK && st >= 0
            && st < NSTEP && no >= 0 && no < 128)
            set_ev(p, tr, st, no, ve, le);
    } else if (!strncmp(ln, "stepdel ", 8)) {
        int p, st, no;
        if (sscanf(ln + 8, "%d %d %d %d", &p, &tr, &st, &no) == 4 && p >= 0
            && p < NPAT && tr >= 0 && tr < NTRACK && st >= 0 && st < NSTEP)
            del_ev(p, tr, st, no);
    } else if (!strncmp(ln, "shift ", 6)) {
        int p, d;
        if (sscanf(ln + 6, "%d %d %d", &p, &tr, &d) == 3 && p >= 0 && p < NPAT
            && tr < NTRACK)
            shift_ev(p, tr, d);
    } else if (!strncmp(ln, "new", 3)) {
        reset_song();
    } else if (sscanf(ln, "clear %d", &tr) == 1 && tr >= 0 && tr < NTRACK) {
        pn[tr] = 0;
    } else if (sscanf(ln, "undo %d", &tr) == 1 && tr >= 0 && tr < NTRACK) {
        if (pn[tr] > 0) pn[tr]--;
    } else if ((path = arg(ln, "samppath"))) {
        snprintf(sample_path, sizeof sample_path, "%s", path);
    } else if (!strncmp(ln, "samprec start", 13)) {
        if (!capturing) {
            sampling = capturing = 1;
            pthread_create(&cap_thr, NULL, capture, NULL);
            pthread_detach(cap_thr);
        }
    } else if (!strncmp(ln, "samprec stop", 12)) {
        sampling = 0;
    } else if (!strncmp(ln, "sampclear", 9)) {
        pthread_mutex_unlock(&mu);
        set_sample(NULL, 0);
        pthread_mutex_lock(&mu);
    } else if ((path = arg(ln, "sampload"))) {
        float *buf;
        char p2[512];
        snprintf(p2, sizeof p2, "%s", path);
        pthread_mutex_unlock(&mu);
        int n = wav_read(p2, &buf);
        if (n > 0) set_sample(buf, n);
        else fprintf(stderr, "engine: cannot load %s\n", p2);
        pthread_mutex_lock(&mu);
    } else if (sscanf(ln, "save %d", &a) == 1) {
        /* "save SEQ PATH": SEQ is echoed in shm once the file is written */
        char *sp = strchr(ln + 5, ' ');
        if (sp) {
            sp[strcspn(sp, "\r\n")] = 0;
            pat_save(sp + 1);
        }
        saved_seq = a;
        shm->saved_seq = a;
    } else if ((path = arg(ln, "load"))) {
        pat_load(path);
    } else if (!strncmp(ln, "quit", 4)) {
        running = 0;
    }
}

static void *cmd_thread(void *arg) {
    (void)arg;
    char ln[600];
    while (fgets(ln, sizeof ln, stdin)) {
        pthread_mutex_lock(&mu);
        handle_cmd(ln);
        pthread_mutex_unlock(&mu);
    }
    running = 0;
    return NULL;
}

int main(void) {
    int fd = open("/dev/shm/zsynth.state", O_RDWR | O_CREAT, 0644);
    if (fd < 0 || ftruncate(fd, sizeof(Shm)) < 0) { perror("shm"); return 1; }
    shm = mmap(NULL, sizeof(Shm), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    memset(shm, 0, sizeof(Shm));
    shm->magic = 0x5a53594e;
    rec_buf = malloc(sizeof(float) * MAXSAMP);
    memset(pend, -1, sizeof pend);
    for (int t = 0; t < NTRACK; t++) {
        P[t][P_CUT] = 12000; P[t][P_ATK] = 0.003f; P[t][P_DEC] = 0.3f;
        P[t][P_SUS] = 0.7f; P[t][P_REL] = 0.2f; P[t][P_VOL] = 0.7f;
    }

    int err;
    pa_sample_spec ss = {PA_SAMPLE_S16LE, SR, 2};
    /* ~20 ms target buffer keeps key-to-sound latency low */
    pa_buffer_attr ba = {(uint32_t)-1, SR / 50 * 4, (uint32_t)-1,
                         BLOCK * 4, (uint32_t)-1};
    pa_simple *pcm = pa_simple_new(NULL, "ZSynth", PA_STREAM_PLAYBACK, NULL,
                                   "synth", &ss, NULL, &ba, &err);
    if (!pcm) {
        fprintf(stderr, "engine: audio open failed: %s\n", pa_strerror(err));
        return 1;
    }
    pthread_t th;
    pthread_create(&th, NULL, cmd_thread, NULL);

    int16_t out[BLOCK * 2];
    int blocks = 0;
    while (running) {
        pthread_mutex_lock(&mu);
        render(out, BLOCK);
        if (++blocks % 4 == 0) publish();
        pthread_mutex_unlock(&mu);
        if (pa_simple_write(pcm, out, sizeof out, &err) < 0) {
            fprintf(stderr, "engine: write failed: %s\n", pa_strerror(err));
            break;
        }
    }
    sampling = 0;
    pa_simple_flush(pcm, &err);
    pa_simple_free(pcm);
    return 0;
}
