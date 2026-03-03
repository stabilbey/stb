import logging
import sqlite3
import threading
import time
from datetime import datetime
from collections import defaultdict
import sys
from types import ModuleType

# Python 3.13 imghdr yaması
if 'imghdr' not in sys.modules:
    sys.modules['imghdr'] = ModuleType('imghdr')
    sys.modules['imghdr'].what = lambda f, h=None: 'jpeg'

import telebot
from telebot import types

# ==================== KONFİGÜRASYON ====================
TOKEN = "8507129536:AAHUaU_1qytkPFUgG-_L6AVwPpDYouaznow"
MAX_SORU = 50
MIN_SURE = 15
MAX_SURE = 60

# Logging sadece hatalar için
logging.basicConfig(level=logging.ERROR)

# Bot nesnesi
bot = telebot.TeleBot(TOKEN)

# ==================== VERİTABANI ====================
db = sqlite3.connect('quiz.db', check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row

# Testler tablosu
db.execute('''CREATE TABLE IF NOT EXISTS tests 
    (id TEXT PRIMARY KEY, 
     user_id INTEGER, 
     name TEXT, 
     count INTEGER, 
     time_limit INTEGER, 
     created TEXT)''')

# Sorular tablosu
db.execute('''CREATE TABLE IF NOT EXISTS questions 
    (id INTEGER PRIMARY KEY, 
     test_id TEXT, 
     no INTEGER, 
     photo TEXT, 
     answer TEXT)''')

# Sonuçlar tablosu (4 yanlış 1 doğruyu götürür)
db.execute('''CREATE TABLE IF NOT EXISTS results
    (id INTEGER PRIMARY KEY, 
     test_id TEXT, 
     user_id INTEGER, 
     name TEXT, 
     correct INTEGER, 
     wrong INTEGER, 
     empty INTEGER, 
     net REAL, 
     date TEXT)''')

# İndexler
db.execute('CREATE INDEX IF NOT EXISTS idx_tests_user ON tests(user_id)')
db.execute('CREATE INDEX IF NOT EXISTS idx_questions_test ON questions(test_id)')
db.execute('CREATE INDEX IF NOT EXISTS idx_results_user ON results(user_id)')
db.execute('CREATE INDEX IF NOT EXISTS idx_results_test ON results(test_id)')

# ==================== SESSIONS ====================
user_sessions = {}  # Kullanıcıların test oluşturma sessionları
sessions = {}  # Kullanıcıların testleri
active_quizzes = {}  # Aktif quizler

# ==================== VERİTABANI FONKSİYONLARI ====================
def get_tests(user_id):
    """Kullanıcının testlerini getir"""
    cursor = db.execute(
        "SELECT id, name, count, time_limit FROM tests WHERE user_id=? ORDER BY created DESC", 
        (user_id,)
    )
    return [(row['id'], row['name'], row['count'], row['time_limit']) for row in cursor]

def get_questions(test_id):
    """Test sorularını getir"""
    cursor = db.execute(
        "SELECT no, photo, answer FROM questions WHERE test_id=? ORDER BY no", 
        (test_id,)
    )
    return {row['no']: (row['photo'], row['answer']) for row in cursor}

def get_test_stats(test_id):
    """Test istatistiklerini getir"""
    cursor = db.execute(
        "SELECT COUNT(*) as total, AVG(net) as avg, MAX(net) as max FROM results WHERE test_id=?", 
        (test_id,)
    ).fetchone()
    return {
        'total': cursor['total'] or 0, 
        'avg': round(cursor['avg'] or 0, 2), 
        'max': round(cursor['max'] or 0, 2)
    }

def save_test(test_id, user_id, name, count, time_limit):
    """Test kaydet"""
    db.execute(
        "INSERT INTO tests VALUES (?,?,?,?,?,?)", 
        (test_id, user_id, name, count, time_limit, datetime.now().strftime("%d.%m.%Y %H:%M"))
    )
    db.commit()

def save_questions(test_id, fotolar, cevaplar):
    """Soruları kaydet"""
    cursor = db.cursor()
    for i, foto in enumerate(fotolar, 1):
        cursor.execute(
            "INSERT INTO questions (test_id, no, photo, answer) VALUES (?,?,?,?)", 
            (test_id, i, foto, cevaplar[i])
        )
    db.commit()

def save_result(test_id, user_id, name, correct, wrong, empty):
    """Sonuç kaydet - NET hesaplama: 4 yanlış 1 doğruyu götürür"""
    net = correct - (wrong / 4)  # 4 yanlış 1 doğruyu götürür
    db.execute(
        "INSERT INTO results (test_id, user_id, name, correct, wrong, empty, net, date) VALUES (?,?,?,?,?,?,?,?)",
        (test_id, user_id, name, correct, wrong, empty, net, datetime.now().strftime("%d.%m.%Y %H:%M"))
    )
    db.commit()
    return net

def delete_test(test_id):
    """Test ve ilişkili tüm verileri sil"""
    db.execute("DELETE FROM questions WHERE test_id=?", (test_id,))
    db.execute("DELETE FROM results WHERE test_id=?", (test_id,))
    db.execute("DELETE FROM tests WHERE id=?", (test_id,))
    db.commit()

def get_user_history(user_id, limit=10):
    """Kullanıcının geçmiş sonuçlarını getir"""
    cursor = db.execute('''SELECT r.date, t.name, r.correct, r.wrong, r.empty, r.net 
                          FROM results r 
                          JOIN tests t ON r.test_id = t.id 
                          WHERE r.user_id=? 
                          ORDER BY r.date DESC LIMIT ?''', (user_id, limit))
    return cursor.fetchall()

# ==================== FORMATLAMA FONKSİYONLARI ====================
def format_result_line(name, correct, wrong, empty, net):
    """Sonuçları tek satırda formatla (sıralama için)"""
    return f"👤 {name}: {correct} D  {wrong} Y  {empty} B  {net:.2f} NET"

def format_detailed_result(name, correct, wrong, empty, net):
    """Detaylı sonuç formatı (kişisel mesaj için)"""
    return (f"📊 *{name}* sonuçların:\n\n"
            f"✅ Doğru: {correct}\n"
            f"❌ Yanlış: {wrong}\n"
            f"⚪ Boş: {empty}\n"
            f"📈 Net: {net:.2f}")

def format_history_line(date, name, correct, wrong, empty, net):
    """Geçmiş sonuçlarını formatla"""
    short_date = date[5:10] if len(date) >= 10 else date
    return f"📅 {short_date} | {name}\n↳ {correct} D  {wrong} Y  {empty} B  {net:.2f} NET"

def create_option_keyboard(owner_id, q_no, selected=None):
    """Seçenek butonlarını oluştur (tik işaretli)"""
    markup = types.InlineKeyboardMarkup(row_width=5)
    buttons = []
    for opt in ['A', 'B', 'C', 'D', 'E']:
        text = f"{opt} ✅" if selected == opt else opt
        buttons.append(types.InlineKeyboardButton(text, callback_data=f"ans_{owner_id}_{q_no}_{opt}"))
    markup.add(*buttons)
    return markup

def create_join_start_keyboard():
    """Katıl ve Başlat butonlarını oluştur"""
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("📝 KATIL", callback_data="join"),
        types.InlineKeyboardButton("🚀 BAŞLAT", callback_data="start_quiz")
    )
    return markup

# ==================== KOMUTLAR ====================
@bot.message_handler(commands=['start'])
def start(message):
    """Başlangıç komutu"""
    bot.reply_to(
        message,
        "🤖 *QUIZ BOT*\n\n"
        "📝 *TEST OLUŞTUR:*\n"
        "/newquiz - Yeni test oluştur\n"
        "/testlerim - Testlerini yönet\n"
        "/gecmis - Geçmiş sonuçların\n\n"
        "👥 *GRUP KOMUTLARI:*\n"
        "/startquiz - Testi başlat\n"
        "/dur - Testi bitir ve sonuçları göster\n"
        "/sonuc - Anlık sonuçlar\n"
        "/iptal - Testi iptal et",
        parse_mode='Markdown'
    )

@bot.message_handler(commands=['newquiz'])
def newquiz(message):
    """Yeni test oluşturma başlangıcı"""
    if message.chat.type != "private":
        bot.reply_to(message, "❌ Bu komut sadece özelde çalışır!")
        return
    
    user_id = message.from_user.id
    user_sessions[user_id] = {'state': 'waiting_name'}
    
    bot.reply_to(message, "📝 Test adı yaz (/atla geç):")

@bot.message_handler(func=lambda message: message.from_user.id in user_sessions and user_sessions[message.from_user.id].get('state') == 'waiting_name')
def process_test_name(message):
    """Test adını işle"""
    user_id = message.from_user.id
    
    if message.text == '/atla':
        name = f"Test {len(get_tests(user_id)) + 1}"
    else:
        name = message.text
    
    user_sessions[user_id].update({
        'state': 'collecting_photos',
        'test_id': datetime.now().strftime("%y%m%d%H%M%S"),
        'test_name': name,
        'fotolar': []
    })
    
    bot.reply_to(message, f"✅ '{name}' oluşturuldu!\n\n📸 Fotoğrafları gönder. Bitince /cevaplar")

@bot.message_handler(content_types=['photo'], func=lambda message: message.from_user.id in user_sessions and user_sessions[message.from_user.id].get('state') == 'collecting_photos')
def handle_photo(message):
    """Fotoğraf işle"""
    user_id = message.from_user.id
    session = user_sessions[user_id]
    
    session['fotolar'].append(message.photo[-1].file_id)
    count = len(session['fotolar'])
    
    if count >= MAX_SORU:
        bot.reply_to(message, f"📸 Maksimum {MAX_SORU} soruya ulaşıldı. /cevaplar yaz.")
    else:
        bot.reply_to(message, f"📸 {count}. foto alındı. Devam et veya /cevaplar")

@bot.message_handler(commands=['cevaplar'], func=lambda message: message.from_user.id in user_sessions)
def cevaplar_command(message):
    """Cevap girişine geç"""
    user_id = message.from_user.id
    session = user_sessions[user_id]
    
    if not session.get('fotolar'):
        bot.reply_to(message, "❌ Önce fotoğraf gönder!")
        return
    
    session['state'] = 'waiting_answers'
    
    bot.reply_to(
        message,
        f"📝 {len(session['fotolar'])} soru için cevapları yaz:\n`1A 2B 3C 4D 5E`",
        parse_mode='Markdown'
    )

@bot.message_handler(func=lambda message: message.from_user.id in user_sessions and user_sessions[message.from_user.id].get('state') == 'waiting_answers')
def process_answers(message):
    """Cevapları işle ve testi kaydet"""
    user_id = message.from_user.id
    session = user_sessions[user_id]
    
    text = message.text.upper().strip()
    fotolar = session['fotolar']
    
    # Cevapları parse et
    cevaplar = {}
    for part in text.split():
        if len(part) >= 2 and part[0].isdigit():
            no = int(''.join(filter(str.isdigit, part)))
            harf = ''.join(filter(str.isalpha, part))
            if harf in 'ABCDE':
                cevaplar[no] = harf
    
    # Kontrol et
    if len(cevaplar) != len(fotolar):
        bot.reply_to(
            message,
            f"❌ {len(fotolar)} soru için {len(cevaplar)} cevap girdin! Tekrar dene:\n`1A 2B 3C 4D 5E`",
            parse_mode='Markdown'
        )
        return
    
    # Veritabanına kaydet
    test_id = session['test_id']
    test_name = session['test_name']
    
    save_test(test_id, user_id, test_name, len(fotolar), 30)
    save_questions(test_id, fotolar, cevaplar)
    
    # Sessions'a ekle
    if user_id not in sessions:
        sessions[user_id] = {}
    
    sessions[user_id][test_id] = {
        'name': test_name,
        'count': len(fotolar),
        'time_limit': 30,
        'questions': {i: (fotolar[i-1], cevaplar[i]) for i in range(1, len(fotolar)+1)}
    }
    
    # Başarılı mesajı
    cevap_metni = ' '.join([f"{no}{harf}" for no, harf in sorted(cevaplar.items())])
    bot.reply_to(
        message,
        f"✅ *{test_name}* kaydedildi!\n\n"
        f"📝 {len(fotolar)} soru\n"
        f"🔑 {cevap_metni}\n"
        f"⏱ Varsayılan süre: 30 sn\n\n"
        f"/testlerim - Testlerini görüntüle\n"
        f"/startquiz - Grupta başlat",
        parse_mode='Markdown'
    )
    
    # Session'ı temizle
    del user_sessions[user_id]

@bot.message_handler(commands=['testlerim'])
def testlerim(message):
    """Testleri listele"""
    user_id = message.from_user.id
    tests = get_tests(user_id)
    
    if not tests:
        bot.reply_to(message, "📭 Testin yok. /newquiz ile oluştur.")
        return
    
    # Sessions'ı güncelle
    if user_id not in sessions:
        sessions[user_id] = {}
    
    # Test butonlarını oluştur
    markup = types.InlineKeyboardMarkup(row_width=1)
    for tid, name, count, tl in tests:
        if tid not in sessions[user_id]:
            sessions[user_id][tid] = {
                'name': name, 
                'count': count, 
                'time_limit': tl,
                'questions': get_questions(tid)
            }
        
        stats = get_test_stats(tid)
        button_text = f"{name} ({count} soru) - {stats['total']} çözüm"
        markup.add(types.InlineKeyboardButton(button_text, callback_data=f"test_{tid}"))
    
    bot.reply_to(message, "📚 *TESTLERİN*", reply_markup=markup, parse_mode='Markdown')

@bot.message_handler(commands=['gecmis'])
def gecmis(message):
    """Geçmiş sonuçları göster"""
    rows = get_user_history(message.from_user.id)
    
    if not rows:
        bot.reply_to(message, "📭 Henüz quiz çözmemişsin!")
        return
    
    text = "📊 *GEÇMİŞ SONUÇLAR*\n\n"
    for row in rows:
        text += format_history_line(
            row['date'], row['name'], 
            row['correct'], row['wrong'], row['empty'], row['net']
        ) + "\n\n"
    
    bot.reply_to(message, text, parse_mode='Markdown')

@bot.message_handler(commands=['startquiz'])
def startquiz(message):
    """Quiz başlatma komutu"""
    user_id = message.from_user.id
    tests = get_tests(user_id)
    
    if not tests:
        bot.reply_to(message, "❌ Önce /newquiz ile test oluştur!")
        return
    
    # Test seçim menüsü
    markup = types.InlineKeyboardMarkup(row_width=1)
    for tid, name, count, tl in tests:
        markup.add(types.InlineKeyboardButton(name, callback_data=f"select_{tid}"))
    
    bot.reply_to(message, "📚 *BAŞLATILACAK TESTİ SEÇ*", reply_markup=markup, parse_mode='Markdown')

@bot.message_handler(commands=['dur', 'bitir'])
def dur(message):
    """Quiz'i bitir"""
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    if chat_id in active_quizzes:
        if active_quizzes[chat_id]['owner_id'] == user_id:
            bot.reply_to(message, "⏹ Quiz bitiriliyor, sonuçlar hesaplanıyor...")
            show_final_results(chat_id, "⏹ *QUİZ ERKEN BİTTİ!*")
        else:
            bot.reply_to(message, "❌ Sadece quiz'i başlatan bitirebilir!")
    else:
        bot.reply_to(message, "❌ Aktif quiz yok!")

@bot.message_handler(commands=['sonuc'])
def sonuc(message):
    """Anlık sonuçları göster"""
    chat_id = message.chat.id
    
    if chat_id not in active_quizzes:
        bot.reply_to(message, "❌ Aktif quiz yok!")
        return
    
    quiz = active_quizzes[chat_id]
    
    if not quiz['participants']:
        bot.reply_to(message, "📭 Henüz katılımcı yok!")
        return
    
    # Anlık sonuçları hesapla
    results = []
    for p in quiz['participants'].values():
        net = p['correct'] - (p['wrong'] / 4)
        results.append((p['name'], p['correct'], p['wrong'], p['empty'], net))
    
    # Net'e göre sırala
    results.sort(key=lambda x: x[4], reverse=True)
    
    # Mesajı oluştur
    text = "📊 *ANLIK SONUÇLAR*\n\n"
    for name, correct, wrong, empty, net in results:
        text += format_result_line(name, correct, wrong, empty, net) + "\n"
    
    bot.reply_to(message, text, parse_mode='Markdown')

@bot.message_handler(commands=['iptal'])
def iptal(message):
    """Quiz'i iptal et"""
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Test oluşturma iptali
    if user_id in user_sessions:
        del user_sessions[user_id]
        bot.reply_to(message, "❌ Test oluşturma iptal edildi!")
        return
    
    # Quiz iptali
    if chat_id in active_quizzes:
        del active_quizzes[chat_id]
        bot.reply_to(message, "❌ Quiz iptal edildi!")
    else:
        bot.reply_to(message, "❌ Aktif quiz yok!")

@bot.message_handler(commands=['atla'])
def atla(message):
    """Atla komutu - sadece test adı için"""
    user_id = message.from_user.id
    if user_id in user_sessions and user_sessions[user_id].get('state') == 'waiting_name':
        process_test_name(message)

# ==================== CALLBACK HANDLER ====================
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    """Tüm callback'leri işle"""
    try:
        if call.data.startswith('select_'):
            select_test(call)
        elif call.data == 'join':
            join_quiz(call)
        elif call.data == 'start_quiz':
            start_quiz_now(call)
        elif call.data.startswith('ans_'):
            handle_answer(call)
        elif call.data.startswith(('test_', 'time_', 'set_', 'stats_', 'del_', 'confirm_', 'back_tests')):
            test_menu(call)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Hata: {str(e)}")

def select_test(call):
    """Test seçildi, bekleme odası oluştur"""
    bot.answer_callback_query(call.id)
    
    user_id = call.from_user.id
    tid = call.data[7:]  # 'select_' sonrası
    
    # Test kontrolü
    if user_id not in sessions or tid not in sessions[user_id]:
        bot.edit_message_text("❌ Test bulunamadı!", call.message.chat.id, call.message.message_id)
        return
    
    t = sessions[user_id][tid]
    chat_id = call.message.chat.id
    
    # Quiz bekleme odası oluştur
    active_quizzes[chat_id] = {
        'owner_id': user_id,
        'test_id': tid,
        'test': t,
        'q_no': 0,
        'participants': {},
        'waiting': False,
        'current_msg_id': None,
        'started': False,
        'question_start_time': None,
        'message_id': call.message.message_id
    }
    
    # Katılımcı listesi ve butonlar
    update_quiz_lobby(call.message, chat_id)

def update_quiz_lobby(message, chat_id):
    """Quiz lobisini güncelle (katılımcı listesi + butonlar)"""
    quiz = active_quizzes.get(chat_id)
    if not quiz:
        return
    
    # Katılımcı listesini oluştur
    if quiz['participants']:
        katilimcilar = "\n".join([f"• {p['name']}" for p in quiz['participants'].values()])
    else:
        katilimcilar = "Henüz kimse katılmadı."
    
    # Mesajı güncelle
    bot.edit_message_text(
        f"🎯 *{quiz['test']['name']}*\n"
        f"📝 {quiz['test']['count']} soru\n"
        f"⏱ Her soru: {quiz['test']['time_limit']} sn\n\n"
        f"📊 *Katılımcılar:*\n{katilimcilar}\n\n"
        f"Katılmak için KATIL butonuna bas,\n"
        f"başlatmak için BAŞLAT'a tıkla!",
        chat_id,
        message.message_id,
        reply_markup=create_join_start_keyboard(),
        parse_mode='Markdown'
    )

def join_quiz(call):
    """Quiz'e katıl"""
    bot.answer_callback_query(call.id)
    
    user = call.from_user
    chat_id = call.message.chat.id
    
    if chat_id not in active_quizzes:
        bot.send_message(chat_id, "❌ Aktif quiz yok!")
        return
    
    quiz = active_quizzes[chat_id]
    
    # Daha önce katılmamışsa ekle
    if user.id not in quiz['participants']:
        quiz['participants'][user.id] = {
            'name': user.first_name,
            'answers': {},
            'correct': 0,
            'wrong': 0,
            'empty': 0
        }
        
        # Lobi mesajını güncelle
        update_quiz_lobby(call.message, chat_id)

def start_quiz_now(call):
    """Quiz'i başlat"""
    bot.answer_callback_query(call.id)
    
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    
    if chat_id not in active_quizzes:
        bot.send_message(chat_id, "❌ Aktif quiz yok!")
        return
    
    quiz = active_quizzes[chat_id]
    
    # Sadece quiz sahibi başlatabilir
    if quiz['owner_id'] != user_id:
        bot.send_message(chat_id, "❌ Sadece quiz'i oluşturan başlatabilir!")
        return
    
    if quiz['started']:
        bot.send_message(chat_id, "❌ Quiz zaten başlamış!")
        return
    
    # Quiz'i başlat
    quiz['started'] = True
    bot.edit_message_text("🚀 *QUİZ BAŞLIYOR!*", chat_id, call.message.message_id, parse_mode='Markdown')
    
    # Biraz bekle ve başlat
    import threading
    threading.Timer(2, run_quiz_loop, args=[chat_id]).start()

def run_quiz_loop(chat_id):
    """Quiz döngüsü - soruları sırayla gönder (thread ile)"""
    quiz = active_quizzes.get(chat_id)
    if not quiz or not quiz.get('started'):
        return
    
    t = quiz['test']
    
    # Her soru için döngü
    for q_no in range(1, t['count'] + 1):
        if chat_id not in active_quizzes or not quiz.get('started'):
            break
        
        # Soruyu gönder
        send_question(chat_id, q_no)
        
        # Süre bekle
        time.sleep(t['time_limit'])
        
        # Soru süresi doldu, cevapları işle
        if chat_id in active_quizzes and quiz.get('started'):
            process_question_results(chat_id, q_no)
    
    # Quiz bitti
    if chat_id in active_quizzes:
        show_final_results(chat_id, "🏁 *QUİZ TAMAMLANDI!*")

def send_question(chat_id, q_no):
    """Tek bir soruyu gönder"""
    quiz = active_quizzes[chat_id]
    t = quiz['test']
    
    quiz['q_no'] = q_no
    quiz['waiting'] = True
    quiz['question_start_time'] = datetime.now()
    
    photo, answer = t['questions'][q_no]
    
    # Soruyu gönder
    msg = bot.send_photo(
        chat_id=chat_id,
        photo=photo,
        caption=f"📝 *Soru {q_no}/{t['count']}*\n⏱ {t['time_limit']} saniye",
        reply_markup=create_option_keyboard(quiz['owner_id'], q_no),
        parse_mode='Markdown'
    )
    
    quiz['current_msg_id'] = msg.message_id

def process_question_results(chat_id, q_no):
    """Soru sonuçlarını işle ve göster"""
    quiz = active_quizzes.get(chat_id)
    if not quiz:
        return
    
    quiz['waiting'] = False
    t = quiz['test']
    dogru_cevap = t['questions'][q_no][1]
    
    # Butonları kaldır
    try:
        bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=quiz['current_msg_id'],
            reply_markup=None
        )
    except:
        pass
    
    # Her katılımcının cevabını işle
    for p in quiz['participants'].values():
        cevap = p['answers'].get(q_no)
        if not cevap:
            p['empty'] += 1
        elif cevap == dogru_cevap:
            p['correct'] += 1
        else:
            p['wrong'] += 1
    
    # Cevap dağılımını hazırla
    votes = {opt: [] for opt in 'ABCDE'}
    for p in quiz['participants'].values():
        ans = p['answers'].get(q_no)
        if ans in votes:
            votes[ans].append(p['name'])
    
    # Sonuç mesajını oluştur
    text = f"📝 *Soru {q_no}/{t['count']}*\n"
    text += f"✅ *Doğru Cevap: {dogru_cevap}*\n\n"
    text += "*Verilen Cevaplar:*\n"
    
    for opt in 'ABCDE':
        if votes[opt]:
            text += f"*{opt}* ({len(votes[opt])}): {', '.join(votes[opt])}\n"
        else:
            text += f"*{opt}*: -\n"
    
    # Boş bırakanlar
    boslar = [p['name'] for p in quiz['participants'].values() if q_no not in p['answers']]
    if boslar:
        text += f"\n*Boş:* {', '.join(boslar)}"
    
    bot.send_message(chat_id=chat_id, text=text, parse_mode='Markdown')
    time.sleep(2)

def show_final_results(chat_id, title):
    """Final sonuçlarını göster"""
    quiz = active_quizzes.pop(chat_id, None)
    if not quiz:
        return
    
    if not quiz['participants']:
        bot.send_message(chat_id=chat_id, text="📭 Kimse katılmadı!")
        return
    
    # Sonuçları hesapla ve kaydet
    results = []
    for user_id, p in quiz['participants'].items():
        net = save_result(
            quiz['test_id'], 
            user_id, 
            p['name'], 
            p['correct'], 
            p['wrong'], 
            p['empty']
        )
        results.append((p['name'], p['correct'], p['wrong'], p['empty'], net))
        
        # Kişiye özel sonuç gönder
        try:
            bot.send_message(
                chat_id=user_id,
                text=format_detailed_result(p['name'], p['correct'], p['wrong'], p['empty'], net),
                parse_mode='Markdown'
            )
        except:
            pass  # Kullanıcı botu engellemiş olabilir
    
    # Net'e göre sırala (büyükten küçüğe)
    results.sort(key=lambda x: x[4], reverse=True)
    
    # Genel sonuç mesajı
    text = f"{title}\n\n"
    for i, (name, correct, wrong, empty, net) in enumerate(results, 1):
        text += f"{i}. {format_result_line(name, correct, wrong, empty, net)}\n"
    
    bot.send_message(chat_id=chat_id, text=text, parse_mode='Markdown')

def handle_answer(call):
    """Cevap butonlarına tıklanınca"""
    user = call.from_user
    
    # Callback data'yı parse et: ans_ownerId_qNo_choice
    data = call.data.split('_')
    owner_id = int(data[1])
    q_no = int(data[2])
    choice = data[3]
    chat_id = call.message.chat.id
    
    # Quiz kontrolü
    if chat_id not in active_quizzes:
        bot.answer_callback_query(call.id, "⏳ Quiz bitti veya başlamadı!", show_alert=True)
        return
    
    quiz = active_quizzes[chat_id]
    
    # Zamanlama kontrolü
    if not quiz.get('started') or not quiz.get('waiting') or quiz['q_no'] != q_no:
        bot.answer_callback_query(call.id, "⏳ Bu soru için süre doldu!", show_alert=True)
        return
    
    # Katılımcı kontrolü
    if user.id not in quiz['participants']:
        bot.answer_callback_query(call.id, "❌ Önce KATIL butonuna bas!", show_alert=True)
        return
    
    p = quiz['participants'][user.id]
    prev_answer = p['answers'].get(q_no)
    
    if prev_answer == choice:
        # Aynı cevap -> iptal et
        del p['answers'][q_no]
        bot.answer_callback_query(call.id, "Cevap iptal edildi!")
        bot.edit_message_reply_markup(
            chat_id,
            call.message.message_id,
            reply_markup=create_option_keyboard(owner_id, q_no)
        )
    else:
        # Yeni cevap veya değiştirme
        p['answers'][q_no] = choice
        bot.answer_callback_query(call.id, f"{choice} seçildi!")
        bot.edit_message_reply_markup(
            chat_id,
            call.message.message_id,
            reply_markup=create_option_keyboard(owner_id, q_no, choice)
        )

def test_menu(call):
    """Test yönetim menüsü"""
    bot.answer_callback_query(call.id)
    
    user_id = call.from_user.id
    data = call.data
    
    # Test detayları
    if data.startswith('test_'):
        tid = data[5:]
        if user_id not in sessions or tid not in sessions[user_id]:
            bot.edit_message_text("❌ Test bulunamadı!", call.message.chat.id, call.message.message_id)
            return
        
        t = sessions[user_id][tid]
        stats = get_test_stats(tid)
        
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("⏱ Süre Ayarla", callback_data=f"time_{tid}"),
            types.InlineKeyboardButton("📊 İstatistikler", callback_data=f"stats_{tid}"),
            types.InlineKeyboardButton("❌ Testi Sil", callback_data=f"del_{tid}"),
            types.InlineKeyboardButton("🔙 Geri Dön", callback_data="back_tests")
        )
        
        bot.edit_message_text(
            f"📚 *{t['name']}*\n\n"
            f"📝 Soru: {t['count']}\n"
            f"⏱ Süre: {t['time_limit']} sn\n"
            f"👥 Çözüm: {stats['total']}\n"
            f"📈 Ortalama Net: {stats['avg']}\n"
            f"🏆 En Yüksek Net: {stats['max']}",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode='Markdown'
        )
    
    # Süre ayarlama
    elif data.startswith('time_'):
        tid = data[5:]
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("15 sn", callback_data=f"set_{tid}_15"),
            types.InlineKeyboardButton("30 sn", callback_data=f"set_{tid}_30"),
            types.InlineKeyboardButton("45 sn", callback_data=f"set_{tid}_45"),
            types.InlineKeyboardButton("60 sn", callback_data=f"set_{tid}_60"),
            types.InlineKeyboardButton("🔙 Geri", callback_data=f"test_{tid}")
        )
        bot.edit_message_text(
            "⏱ *SÜRE AYARLA*",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode='Markdown'
        )
    
    # Süreyi kaydet
    elif data.startswith('set_'):
        parts = data.split('_')
        tid = parts[1]
        sec = int(parts[2])
        
        if user_id in sessions and tid in sessions[user_id]:
            sessions[user_id][tid]['time_limit'] = sec
            db.execute("UPDATE tests SET time_limit=? WHERE id=?", (sec, tid))
            db.commit()
            
            bot.edit_message_text(
                f"✅ Süre {sec} saniye olarak ayarlandı!",
                call.message.chat.id,
                call.message.message_id
            )
            
            # Test menüsüne dön
            call.data = f"test_{tid}"
            test_menu(call)
    
    # İstatistikler
    elif data.startswith('stats_'):
        tid = data[6:]
        stats = get_test_stats(tid)
        t = sessions[user_id][tid]
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Geri", callback_data=f"test_{tid}"))
        
        bot.edit_message_text(
            f"📊 *{t['name']} İstatistikleri*\n\n"
            f"👥 Toplam Katılımcı: {stats['total']}\n"
            f"📈 Ortalama Net: {stats['avg']}\n"
            f"🏆 En Yüksek Net: {stats['max']}\n"
            f"📝 Toplam Soru: {t['count']}",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode='Markdown'
        )
    
    # Silme onayı
    elif data.startswith('del_'):
        tid = data[4:]
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("✅ EVET SİL", callback_data=f"confirm_{tid}"),
            types.InlineKeyboardButton("❌ HAYIR", callback_data=f"test_{tid}")
        )
        bot.edit_message_text(
            "⚠️ *TEST SİLİNSİN Mİ?*\nBu işlem geri alınamaz!",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode='Markdown'
        )
    
    # Silme onaylandı
    elif data.startswith('confirm_'):
        tid = data[8:]
        if user_id in sessions and tid in sessions[user_id]:
            delete_test(tid)
            del sessions[user_id][tid]
        
        bot.edit_message_text(
            "✅ Test silindi!",
            call.message.chat.id,
            call.message.message_id
        )
        
        # Test listesine dön
        testlerim(call.message)

# ==================== MAIN ====================
if __name__ == '__main__':
    print("🚀 QUIZ BOT BAŞLATILIYOR... (telebot)")
    print("📊 4 YANLIŞ 1 DOĞRUYU GÖTÜRÜR")
    print("✅ BOT HAZIR!")
    print("📌 ÖZELLİKLER:")
    print("   • 4 yanlış 1 doğruyu götürür (eksi net mümkün)")
    print("   • Katıl + Başlat sistemi")
    print("   • Butonlarda tik işareti")
    print("   • Detaylı sonuçlar")
    
    try:
        bot.infinity_polling()
    except KeyboardInterrupt:
        print("\n👋 Bot durduruldu!")
    except Exception as e:
        print(f"❌ Hata: {e}")
