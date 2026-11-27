# Investigasi: progres tidak tersimpan dan unit terkunci

Per 28 November 2026.

## Ringkasan

Game berjalan penuh di browser modern tanpa Flash, tetapi **progres gameplay
tidak tersimpan sama sekali** — dan penyebabnya bukan yang semula diduga.

Tiga koreksi terhadap pemahaman sebelumnya:

1. Perbaikan `record_stats.php` (sesi ditulis ulang pada tiap request aset)
   nyata dan terukur, tetapi **bukan** penyebab progres hilang.
2. Penyebab sebenarnya lebih hulu: klien **tidak pernah mengirim transaksi apa
   pun** setelah empat transaksi startup. Aksi gameplay tidak sampai ke server,
   jadi tidak ada yang bisa disimpan.
3. Teori "unit terkunci karena `unlockedContracts` kosong" **terbantah**. Save
   yang sudah berisi `U01` tetap menampilkan Cadet Soldiers bergembok.

Dua keluhan yang tampak terpisah — progres hilang dan troops terkunci —
kemungkinan besar berakar sama.

## Temuan 1: progres benar-benar tidak tersimpan

Direproduksi pada `master` terbaru, bukan sisa dari server versi lama.

| Langkah | Yang dilaporkan klien | Yang dilaporkan server |
| --- | --- | --- |
| Login ke save "Uji Barracks" | — | coins 5000 |
| Beli Barracks (200 koin) | coins 4800, fondasi terpasang | coins 5000 |
| Reload halaman | kembali ke tutorial awal | coins 5000, Barracks hilang |

Pembelian hanya hidup di layar. Setelah reload, save kembali ke kondisi semula
dan tutorial mengulang dari awal. Uji terpisah sebelumnya konsisten: sesi
pertama membeli Barracks seharga 200, sesi berikutnya tetap membaca 5000.

## Temuan 2: antrean transaksi klien berhenti setelah startup

Ini temuan terpenting. Aksi gameplay **tidak pernah sampai ke server**.

Dari log konsol Ruffle, `TransactionManager` hanya membuat empat transaksi,
semuanya saat startup:

1. `TInitUser`
2. `TSetCurrentCampaign`
3. `TInitRequestFriends`
4. `TGetUsersInvaderChallenges`

Setelah itu berhenti total. Membeli bangunan, menempatkan fondasi, dan mengklik
lokasi konstruksi tidak menghasilkan satu pun baris `Adding transaction` baru,
bahkan setelah ditunggu 20 detik.

Log server mengonfirmasi dari sisi lain: hanya dua batch gateway yang masuk,
keduanya dari startup. Nol panggilan AMF untuk aksi gameplay.

Konsekuensinya: tidak ada yang bisa disimpan karena tidak ada yang dikirim. Ini
menjelaskan kenapa perbaikan di sisi penyimpanan sesi tidak mengubah apa pun
yang dirasakan pemain.

## Temuan 3: dua kesimpulan sebelumnya perlu dikoreksi

**Teori `unlockedContracts` salah.** Panel Barracks berhasil dibuka pada save
yang sudah berisi `["U01","U43","U66"]`, dan Cadet Soldiers tetap bergembok.
PR #5 tidak menyelesaikan masalah troops; field itu bukan gerbangnya.

Kandidat berikutnya, `unlockedUnits` dan `unlockedItems`, sempat diisi sebagai
eksperimen. Keduanya masuk ke save, tetapi pengujiannya tidak tuntas karena
progres keburu hilang sebelum panel bisa dibuka lagi. Eksperimen itu sudah
dibatalkan dan tidak tertinggal di repo.

**Perbaikan `record_stats.php` tetap benar, tapi bukan akar masalah.**
Pengukurannya sahih: satu `POST /record_stats.php` memindahkan `expiry` baris
sesi dari `22:58:15.380` ke `22:58:24.940` sebelum perbaikan, dan tidak berubah
sesudahnya. Perbaikan itu menghapus satu kelas bug nyata (save multi-MB ditulis
ulang tiap request aset), hanya saja bukan yang pemain rasakan.

Pelajarannya: yang terbukti waktu itu hanya perilaku HTTP, bukan
"main → reload → progres bertahan".

## Hipotesis utama

**Tutorial mandek di langkah "click on it three times to finish building", dan
tutorial menggerbangi seluruh alur permainan.**

Kalau konstruksi tidak pernah selesai, game tidak pernah meng-commit transaksi,
tidak ada yang disimpan, dan unit tidak pernah terbuka. Satu akar, dua gejala.

Bukti pendukung:

- Klik pada lokasi konstruksi tidak menghasilkan apa pun: tidak ada progres,
  tooltip, maupun transaksi.
- Teks tutorial tidak pernah berganti meski diklik berkali-kali di beberapa
  titik koordinat.
- Transaksi berhenti persis setelah empat transaksi startup, yaitu sebelum aksi
  pemain pertama.

Bukti yang melawan:

- Klik lain berfungsi: menu Build terbuka, pembelian berhasil, fondasi bisa
  ditempatkan, dan panel Barracks bisa dibuka. Input Ruffle tidak mati total.
- Pemain lain tampak sudah melewati tutorial: pulaunya punya bangunan jadi dan
  unit tertempatkan.

Karena itu hipotesis ini belum pasti. Bisa jadi masalah spesifik pada satu
lingkungan, atau memang gerbang tutorial yang sama yang dilewati dengan cara
lain, misalnya lewat save editor.

## Rencana lanjutan

Urutannya menaruh yang termurah dan paling menentukan di depan. Setiap langkah
punya kriteria lulus, supaya tidak ada lagi kesimpulan tanpa bukti end-to-end.

**Langkah 0 — konfirmasi pemain (blokir, 2 menit).** Apakah "klik 3× untuk
menyelesaikan bangunan" pernah benar-benar berhasil, dan apakah ada aksi yang
bertahan setelah reload? Memisahkan "bug di satu lingkungan" dari "bug nyata di
gerbang tutorial".
*Lulus bila:* arah penyelidikan (klien atau server) sudah pasti.

**Langkah 1 — petakan transaksi yang seharusnya terjadi.** Telusuri
`WorldService.performAction` dan handler terkait di `empires-server.py`, lalu
bandingkan dengan transaksi yang benar-benar dikirim klien.
*Lulus bila:* nama transaksi yang hilang saat bangunan diklik bisa disebutkan.

**Langkah 2 — uji apakah antrean macet atau tidak pernah terisi.** Pantau log
`TransactionManager` sambil melakukan aksi lain yang pasti menghasilkan
transaksi, misalnya memanen sumber daya atau mengunjungi tetangga.
*Lulus bila:* terbukti apakah masalahnya khusus konstruksi atau seluruh aksi.

**Langkah 3 — perbaiki akarnya, lalu uji end-to-end.** Kriteria kelulusan bukan
lagi status HTTP, melainkan: lakukan aksi, reload, nilai di
`/admin/status.json` tetap berubah.
*Lulus bila:* satu aksi gameplay bertahan melewati reload.

**Langkah 4 — kembali ke troops.** Setelah transaksi mengalir, ulangi uji
Barracks dan cari field yang benar-benar menggerbangi unit (`unlockedUnits`,
`unlockedItems`, atau state kontrak per bangunan).
*Lulus bila:* Cadet Soldiers bisa dilatih, terlihat di layar.

**Langkah 5 — revert bila perlu.** Kalau PR #5 terbukti tidak relevan,
kembalikan supaya save awal tidak membawa perubahan tanpa dasar.

## Yang belum diketahui

- Mengapa klik pada objek konstruksi tidak menghasilkan transaksi, padahal klik
  lain berfungsi.
- Field mana yang sebenarnya menggerbangi unit di panel Barracks.
- Apakah gejalanya sama di semua lingkungan atau khusus satu sesi.

## Cara mereproduksi

```bash
./serve.sh --port 5087
```

1. Daftar akun baru di `/register.html`, isi nama pulau.
2. Catat `coins` dari `/admin/status.json`.
3. Di game: buka menu Build, beli Barracks, tempatkan fondasinya.
4. Tunggu 20 detik, lalu periksa `coins` lagi di `/admin/status.json`.
5. Reload halaman dan periksa apakah bangunannya masih ada.

Untuk melihat antrean transaksi, buka konsol browser dan saring dengan
`TransactionManager`. Untuk sisi server, saring log dengan `Gateway:`.
