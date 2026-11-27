# Status

Per 28 November 2026.

Game sudah berjalan penuh di browser modern tanpa Adobe Flash. Yang masih rusak:
**progres gameplay tidak tersimpan**, dan unit dasar di Barracks tetap terkunci.
Rinciannya di [investigasi progres tidak tersimpan](investigation-progress-not-saved.md).

## Cara menjalankan

```bash
./serve.sh                 # http://127.0.0.1:5005/
./serve.sh --port 8080     # flag empires-server.py diteruskan apa adanya
SKIP_INSTALL=1 ./serve.sh  # lewati pemeriksaan dependensi saat restart
```

`serve.sh` membuat/memakai ulang `.venv`, memasang `requirements.txt`, membuat
`instance/` (tanpa itu checkout baru mati dengan `sqlite3.OperationalError:
unable to open database file`), dan menjalankan server dengan `--no-popup`.

Halaman penting:

| URL | Isi |
| --- | --- |
| `/` | Game, layar penuh. Mengalihkan ke `/login.html` bila ada save tapi sesi kosong |
| `/login.html`, `/register.html` | Akun: username, password, nama pulau |
| `/admin` | Konsol uji: status sesi, cheat, quest, patch save |
| `/save-editor` | Editor save bawaan proyek |
| `/pick-save.html` | Pemilih save lama, untuk pengujian |
| `/ruffle-classic.html`, `/home-classic.html`, `/index-classic.html` | Halaman sebelum rework |

## Yang sudah masuk master

| PR | Isi | Status bukti |
| --- | --- | --- |
| #1 | Jalan tanpa Flash: `allowScriptAccess`, self-host Ruffle 0.8.0, `swf_patch.py`, `serve.sh` | Terverifikasi di layar |
| #2 | Save editor tidak crash saat belum ada save | Terverifikasi |
| #3 | Halaman game layar penuh dan jadi home, halaman lama di-backup | Terverifikasi di layar |
| #4 | `/record_stats.php` dkk tidak lagi menulis ulang save tiap request | Terukur, tapi bukan akar masalah |
| #5 | Penanaman kontrak starter (`U01`, `U43`, `U66`) | Terverifikasi di data, **gagal di layar** |
| #6 | Login dan register berbasis akun, nama pulau masuk ke save | Terverifikasi end-to-end |

## Masalah terbuka

1. **Progres tidak tersimpan.** Klien berhenti mengirim transaksi setelah empat
   transaksi startup, jadi aksi gameplay tidak pernah sampai ke server. Ini yang
   paling menghambat; lihat dokumen investigasi.
2. **Unit dasar Barracks terkunci.** Cadet Soldiers tetap bergembok walau
   `unlockedContracts` sudah berisi `U01`. Field penggerbangnya belum ketahuan.
3. **PR #5 perlu ditinjau ulang.** Dibuat atas teori yang kini terbantah, dan
   mengubah isi save awal. Mungkin perlu dikembalikan.

## Catatan keamanan

- `/admin` tanpa autentikasi, sesuai permintaan saat dibuat. Sistem akun kini
  sudah ada, jadi bisa dipasangkan kapan saja.
- Jangan jalankan dengan `--host 0.0.0.0` selama `/admin` masih terbuka: tidak
  ada autentikasi, dan beberapa route yang mengubah save menerima GET.
- `--debug` menyalakan debugger Werkzeug. Aman di localhost, berbahaya bila
  digabung dengan host non-lokal.
- Jangan memuat `save.db` atau savespack dari sumber yang tidak dipercaya:
  save dibaca dengan `pickle`, yang berarti eksekusi kode.
