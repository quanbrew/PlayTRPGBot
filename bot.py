import datetime
import io
import logging
import os
import pickle
import re
import uuid
from typing import Optional

import django
import telegram
from dotenv import load_dotenv
# from telegram.ext.dispatcher import run_async
from redis import Redis
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackQueryHandler, JobQueue

import dice

load_dotenv()
django.setup()

from archive.models import Chat, Log, LogKind  # noqa


# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
redis = Redis(host='redis', port=6379, db=0)
DEFAULT_FACE = 100
BUFFER_TIME = 20
TOKEN = os.environ['BOT_TOKEN']
GM_SYMBOL = '✧'

logger = logging.getLogger(__name__)


help_file = open('./help.md')
start_file = open('./start.md')
HELP_TEXT = help_file.read()
START_TEXT = start_file.read()
help_file.close()
start_file.close()


class Round:
    reply_markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("下一轮", callback_data='round:next'),
            InlineKeyboardButton("上一轮", callback_data='round:prev'),
        ],
        [
            InlineKeyboardButton("删除当前", callback_data='round:remove'),
            InlineKeyboardButton("结束", callback_data='round:finish'),
        ]
    ])

    def __init__(self, chat: telegram.Chat):
        text = '回合指示器\n\n没有人加入回合，使用 <code>.init [值]</code> 来加入回合'
        message = chat.send_message(text, parse_mode='HTML', reply_markup=self.reply_markup)
        bot = chat.bot
        assert isinstance(bot, telegram.Bot)
        bot.pin_chat_message(chat.id, message.message_id, disable_notification=True)
        self.count = 0
        self.chat_id = chat.id
        self.message_id = message.message_id
        self.actors = []

    def add_actor(self, actor_name, value):
        self.actors.append((value, actor_name))
        self.actors.sort(reverse=True)

    def remove_current(self):
        if len(self.actors) > 1:
            self.actors.pop(self.count % len(self.actors))

    @staticmethod
    def redis_key(chat_id):
        return 'chat:{}:round'.format(chat_id)

    @staticmethod
    def get(chat_id):
        pickled = redis.get(Round.redis_key(chat_id))
        if not pickled:
            return None
        else:
            return pickle.loads(pickled)

    @staticmethod
    def clear(chat_id):
        redis.delete(Round.redis_key(chat_id))

    def finish(self, query: telegram.CallbackQuery):
        query.edit_message_text('回合轮已结束')
        Round.clear(self.chat_id)
        if isinstance(query.bot, telegram.Bot):
            query.bot.unpin_chat_message(chat_id=self.chat_id)

    def save(self):
        redis.set(Round.redis_key(self.chat_id), pickle.dumps(self))

    def next(self):
        self.count += 1

    def prev(self):
        if self.count > 0:
            self.count -= 1

    def refresh(self, query=None, bot=None):
        text = '<b>回合指示器: {}</b>\n\n'.format(self.count + 1)
        actors_count = len(self.actors)
        for i in range(actors_count):
            value, name = self.actors[i]
            is_current = self.count % actors_count == i
            if is_current:
                text += '◦ {} ({}) ← 当前\n'.format(name, value)
            else:
                text += '◦ {} ({})\n'.format(name, value)

        if isinstance(query, telegram.CallbackQuery):
            query.edit_message_text(text, parse_mode='HTML', reply_markup=self.reply_markup)
        elif isinstance(bot, telegram.Bot):
            bot.edit_message_text(
                text,
                chat_id=self.chat_id,
                message_id=self.message_id,
                parse_mode='HTML',
                reply_markup=self.reply_markup
            )


def start(_, update, job_queue):
    """Send a message when the command /start is issued."""
    message = update.message
    assert isinstance(message, telegram.Message)
    if message.chat.type != 'supergroup':
        message.reply_text(START_TEXT, parse_mode='Markdown')
        return
    chat = get_chat(message.chat)
    if not chat.recording:
        chat.recording = True
        chat.save()
        message.chat.send_message('已重新开始记录，输入 /save 告一段落')
    else:
        error_message(message, job_queue, '已经正在记录了')


def save(_, update, job_queue):
    message = update.message
    assert isinstance(message, telegram.Message)
    if message.chat.type != 'supergroup':
        return
    chat = get_chat(message.chat)
    if chat.recording:
        chat.recording = False
        chat.save_date = datetime.datetime.now()
        chat.save()
        message.chat.send_message('告一段落，在 /start 前我不会再记录')
    else:
        error_message(message, job_queue, '已经停止记录了')


def delete_message(message: telegram.Message):
    try:
        message.delete()
    except telegram.error.TelegramError:
        message.reply_text('删除消息失败，请检查一下 Bot 的权限设置')


def bot_help(_, update):
    """Send a message when the command /help is issued."""
    update.message.reply_text(HELP_TEXT, parse_mode='Markdown')


def set_temp_name(chat_id, user_id, temp_name):
    redis.set('chat:{}:user:{}:name:temp'.format(chat_id, user_id), temp_name.encode())


def get_temp_name(chat_id, user_id):
    result = redis.get('chat:{}:user:{}:name:temp'.format(chat_id, user_id))
    if result:
        return result.decode()


def start_round(_, update: telegram.Update):
    message = update.message
    assert isinstance(message, telegram.Message)
    game_round = Round(message.chat)
    game_round.save()


def set_name(_, update: telegram.Update, args, job_queue):
    message = update.message
    assert isinstance(message, telegram.Message)
    if len(args) == 0:
        return error_message(message, job_queue, '请在 /name 后写下你的角色名')
    user = message.from_user
    assert isinstance(user, telegram.User)
    name = ' '.join(args).strip()
    redis.set('chat:{}:user:{}:name'.format(message.chat_id, user.id), name.encode())
    message.chat.send_message('{} 已被设为 {}'.format(user.full_name, name))
    delete_message(message)
    save_username(message.chat_id, message.from_user.username, name)


def get_name(message: telegram.Message) -> Optional[str]:
    user_id = message.from_user.id
    name = redis.get('chat:{}:user:{}:name'.format(message.chat_id, user_id))
    if name:
        return name.decode()
    else:
        return None


def set_dice_face(_, update, args, job_queue):
    message = update.message
    assert isinstance(message, telegram.Message)
    if len(args) != 1:
        return error_message(
            message,
            job_queue,
            '需要（且仅需要）指定骰子的默认面数，'
            '目前为 <b>{}</b>'.format(get_default_dice_face(chat_id=message.chat_id))
        )
    try:
        face = int(args[0])
    except ValueError:
        error_message(message, job_queue, '面数只能是数字')
        return
    redis.set('chat:{}:face'.format(message.chat_id), face)


def get_default_dice_face(chat_id) -> int:
    try:
        return int(redis.get('chat:{}:face'.format(chat_id)))
    except (ValueError, TypeError):
        return DEFAULT_FACE


def roll_text(chat_id, text):
    _, text = dice.roll(text, get_default_dice_face(chat_id))
    return text


def inline_callback(_, update):
    query = update.callback_query
    assert isinstance(query, telegram.CallbackQuery)
    data = query.data or ''
    data = str(data)
    if data.startswith('round'):
        game_round = Round.get(query.message.chat_id)
        if not isinstance(game_round, Round):
            query.answer(show_alert=True, text='现在游戏没在回合状态之中')
            return
        if data.startswith('round:next'):
            game_round.next()
            game_round.refresh(query=query)
            game_round.save()
        elif data.startswith('round:prev'):
            game_round.prev()
            game_round.refresh(query=query)
            game_round.save()
        elif data.startswith('round:remove'):
            game_round.remove_current()
            game_round.refresh(query=query)
            game_round.save()
        elif data.startswith('round:finish'):
            game_round.finish(query)
        return
    else:
        # hide roll
        result = redis.get('roll:{}'.format(query.data))
        if result:
            data = pickle.loads(result)
            if is_gm(data['chat_id'], query.from_user.id):
                text = data['text'].replace('<code>', '').replace('</code>', '')
            else:
                text = '你不是 GM，不能看哟'
        else:
            text = '找不到这条暗骰记录'
        query.answer(
            show_alert=True,
            text=text,
            cache_time=10000,
        )


def handle_roll(message: telegram.Message, name: str, text: str,
                job_queue: JobQueue, hide=False):
    if text.strip() == '':
        text = 'd'
    try:
        result_text = roll_text(message.chat_id, text)
    except dice.RollError as e:
        return error_message(message, job_queue, e.args[0])
    kind = LogKind.ROLL.value
    if hide:
        roll_id = str(uuid.uuid4())
        redis.set('roll:{}'.format(roll_id), pickle.dumps({
            'text': result_text,
            'chat_id': message.chat_id,
        }))
        keyboard = [[InlineKeyboardButton("GM 查看", callback_data=roll_id)]]

        reply_markup = InlineKeyboardMarkup(keyboard)
        sent = message.chat.send_message(
            '<b>{}</b> 投了一个隐形骰子'.format(name),
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        kind = LogKind.HIDE_DICE.value
    else:
        sent = message.chat.send_message(
            '{} 🎲 {}'.format(name, result_text),
            parse_mode='HTML'
        )
    user = message.from_user
    assert isinstance(user, telegram.User)
    chat = get_chat(message.chat)
    if chat.recording:
        Log.objects.create(
            user_id=user.id,
            message_id=sent.message_id,
            chat=chat,
            content=result_text,
            user_fullname=user.full_name,
            character_name=name,
            gm=is_gm(message.chat_id, user.id),
            kind=kind,
            created=message.date,
        )
    context = dict(
        chat_id=message.chat_id,
        message_id_list=[message.message_id]
    )
    job_queue.run_once(delay_delete_messages, 10, context)


ME_REGEX = re.compile(r'^[.。]me\b|\s[.。]me\s?')
USERNAME_REGEX = re.compile(r'@([a-zA-Z0-9_]{5,})')


def is_author(message_id, user_id):
    return bool(Log.objects.filter(message_id=message_id, user_id=user_id).first())


def get_name_by_username(chat_id, username):
    name = redis.get('chat:{}:username:{}:name'.format(chat_id, username))
    if name:
        return name.decode()
    else:
        return None


def delay_delete_messages(bot: telegram.Bot, job):
    chat_id = job.context['chat_id']
    for message_id in job.context['message_id_list']:
        bot.delete_message(chat_id, message_id)


def error_message(message: telegram.Message, job_queue: JobQueue, text: str):
    delete_time = 15
    sent = message.reply_text('<b>[ERROR]</b> {}'.format(text), parse_mode='HTML')
    context = dict(
        chat_id=message.chat_id,
        message_id_list=(message.message_id, sent.message_id),
    )
    job_queue.run_once(delay_delete_messages, delete_time, context=context)


def get_symbol(chat_id, user_id) -> str:
    symbol = ''
    if is_gm(chat_id, user_id):
        symbol = GM_SYMBOL
    return symbol + ' '


def is_empty_message(text):
    return ME_REGEX.sub('', text).strip() == ''


# ..(space)..[name];..(space)..
AS_PATTERN = re.compile(r'^\s*([^;]+)[;；]\s*')


def handle_as_say(bot: telegram.Bot, chat, job_queue, message: telegram.Message,
                  text: str, with_photo=None):
    user_id = message.from_user.id
    match = AS_PATTERN.match(text)
    if match:
        name = match.group(1).strip()
        if name == '':
            return error_message(message, job_queue, '名字不能为空')
        set_temp_name(chat.chat_id, user_id, name)
        text = text[match.end():]
    if not is_gm(chat.chat_id, user_id):
        return error_message(message, job_queue, '.as 命令只有 GM 能用')
    else:
        name = get_temp_name(chat.chat_id, user_id) or ''
        if name == '':
            error_text = '''.as 的用法是 .as [名字]; [内容]。
如果之前用过 .as 的话可以省略名字的部分，直接写 .as [内容]。
但你之前并没有用过 .as'''
            return error_message(message, job_queue, error_text)

    handle_say(bot, chat, job_queue, message, name, text, with_photo=with_photo)


def handle_say(bot: telegram.Bot, chat, job_queue, message: telegram.Message,
               name: str, text: str, edit_log=None, with_photo=None):
    user_id = message.from_user.id
    gm = is_gm(message.chat_id, user_id)

    # process input text
    def name_resolve(match):
        username = match.group(1)
        name_result = get_name_by_username(message.chat_id, username)
        if not name_result:
            return '@{}'.format(username)
        return '<b>{}</b>'.format(name_result)

    text = USERNAME_REGEX.sub(name_resolve, text)
    kind = LogKind.NORMAL.value

    if is_empty_message(text):
        error_message(message, job_queue, '不能有空消息')
        return
    elif ME_REGEX.search(text):
        send_text = ME_REGEX.sub('<b>{}</b>'.format(name), text)
        content = send_text
        kind = LogKind.ME.value
    else:
        send_text = '<b>{}</b>: {}'.format(name, text)
        content = text
    symbol = get_symbol(message.chat_id, user_id)
    send_text = symbol + send_text
    # on edit
    if edit_log:
        assert isinstance(edit_log, Log)
        edit_log.content = content
        edit_log.kind = kind
        edit_log.save()
        bot.edit_message_text(send_text, message.chat_id, edit_log.message_id, parse_mode='HTML')
        delete_message(message)
        return

    # send message or photo
    reply_to_message_id = None
    reply_log = None
    target = message.reply_to_message
    if isinstance(target, telegram.Message):
        reply_to_message_id = target.message_id
        reply_log = Log.objects.filter(chat=chat, message_id=reply_to_message_id).first()
    if isinstance(with_photo, telegram.PhotoSize):
        sent = message.chat.send_photo(
            photo=with_photo,
            caption=send_text,
            reply_to_message_id=reply_to_message_id,
            parse_mode='HTML',
        )
    else:
        if not chat.recording:
            send_text = '[未记录] ' + send_text
        sent = message.chat.send_message(
            send_text,
            reply_to_message_id=reply_to_message_id,
            parse_mode='HTML',
        )

    if chat.recording:
        # record log
        created_log = Log.objects.create(
            message_id=sent.message_id,
            chat=chat,
            user_id=user_id,
            user_fullname=message.from_user.full_name,
            kind=kind,
            reply=reply_log,
            character_name=name,
            content=content,
            gm=gm,
            created=message.date,
        )
        # download and write photo file
        if isinstance(with_photo, telegram.PhotoSize):
            created_log.media.save('{}.jpeg'.format(uuid.uuid4()), io.BytesIO(b''))
            media = created_log.media.open('rb+')
            with_photo.get_file().download(out=media)
            media.close()
    delete_message(message)


def handle_delete(message: telegram.Message, job_queue):
    target = message.reply_to_message
    if isinstance(target, telegram.Message):
        user_id = message.from_user.id
        log = Log.objects.filter(message_id=target.message_id).first()
        if log is None:
            error_message(message, job_queue, '这条记录不存在于数据库')
        elif log.user_id == user_id or is_gm(message.chat_id, user_id):
            delete_message(target)
            delete_message(message)
            log.deleted = True
            log.save()
        else:
            error_message(message, job_queue, '你没有删掉这条记录的权限')
    else:
        error_message(message, job_queue, '回复需要删除的记录')


def handle_edit(bot, chat, job_queue, message: telegram.Message, text: str):
    target = message.reply_to_message
    if not isinstance(target, telegram.Message):
        return error_message(message, job_queue, '回复需要编辑的记录')

    assert isinstance(message.from_user, telegram.User)
    user_id = message.from_user.id
    log = Log.objects.filter(chat=chat, message_id=target.message_id).first()
    if log is None:
        error_message(message, job_queue, '找不到对应的消息')
    elif log.user_id == user_id:
        handle_say(bot, chat, job_queue, message, log.character_name, text, edit_log=log)
        delete_message(message)
    else:
        error_message(message, job_queue, '你没有编辑这条消息的权限')


INITIATIVE_REGEX = re.compile(r'^(.+)=\s*(\d{1,4})$')


def handle_initiative(message: telegram.Message, job_queue, name: str, text: str):
    text = text.strip()
    match = INITIATIVE_REGEX.match(text)
    number = text
    if match is not None:
        name = match.group(1).strip()
        number = match.group(2)
    elif not text.isnumeric() or len(text) > 4:
        error_message(message, job_queue, '请输入四位数以内的数字，或用等号分开名字和数字')
        return
    game_round = Round.get(message.chat_id)
    if not isinstance(game_round, Round):
        error_message(message, job_queue, '请先用 /round 指令开启回合轮')
        return
    game_round.add_actor(name, int(number))
    game_round.save()
    game_round.refresh(bot=message.bot)
    message.delete()


def handle_lift(update: telegram.Update, job_queue):
    message = update.message
    assert isinstance(message, telegram.Message)
    reply_to = message.reply_to_message
    user_id = message.from_user.id
    if not isinstance(reply_to, telegram.Message):
        return error_message(message, job_queue, '需要回复一条消息来转换')
    elif reply_to.from_user.id == message.bot.id:
        return error_message(message, job_queue, '需要回复一条玩家发送的消息')
    elif reply_to.from_user.id == user_id or is_gm(message.chat_id, user_id):
        update.message = reply_to
        delete_message(update.message)
        return handle_message(message.bot, update, job_queue, lift=True)
    else:
        return error_message(message, job_queue, '你只能转换自己的消息，GM 能转换任何人的消息')


def is_gm(chat_id: int, user_id: int) -> bool:
    return redis.sismember('chat:{}:admin_set'.format(chat_id), user_id)


def update_admin_job(bot, job):
    chat_id = job.context
    try:
        administrators = bot.get_chat_administrators(chat_id)
        user_id_list = [member.user.id for member in administrators]
        admin_set_key = 'chat:{}:admin_set'.format(chat_id)
        redis.delete(admin_set_key)
        if user_id_list:
            redis.sadd(admin_set_key, *user_id_list)
        else:
            job.schedule_removal()
    except TelegramError:
        job.schedule_removal()
        return


def save_username(chat_id, username=None, name=None):
    if username and name:
        redis.set('chat:{}:username:{}:name'.format(chat_id, username), name)


def run_chat_job(_, update, job_queue):
    assert isinstance(job_queue, telegram.ext.JobQueue)
    if isinstance(update.message, telegram.Message):
        message = update.message
        chat_type = message.chat.type
        if chat_type not in ('supergroup', 'group'):
            return
        chat_id = message.chat_id
        job_name = 'chat:{}'.format(chat_id)
        job = job_queue.get_jobs_by_name(job_name)
        if not job:
            job_queue.run_repeating(
                update_admin_job,
                interval=8,
                first=1,
                context=chat_id,
                name=job_name
            )


COMMAND_REGEX = re.compile(r'^[.。](me\b|r|roll|del|delete|edit\b|init|hd|lift|sub|as)?\s*')


def handle_message(bot, update, job_queue, lift=False):
    message = update.message
    assert isinstance(message, telegram.Message)
    with_photo = handle_photo(message)
    if with_photo:
        text = message.caption_html_urled
    else:
        text = message.text_html_urled
    if not isinstance(text, str):
        return
    elif lift:
        text = '.' + text
    message_match = COMMAND_REGEX.match(text)
    if not message_match:
        return
    elif message.chat.type != 'supergroup' and message.chat.type != 'group':
        message.reply_text('只能在群中使用我哦')
        return
    elif not isinstance(message.from_user, telegram.User):
        return
    command = message_match.group(1)
    chat = get_chat(message.chat)
    name = get_name(message)
    if not name:
        error_message(message, job_queue, '请先使用 <code>/name [你的角色名]</code> 设置角色名')
        return
    rest = text[message_match.end():]

    if command == 'r' or command == 'roll':
        handle_roll(message, name, rest, job_queue)
    elif command == 'me':
        handle_say(bot, chat, job_queue, message, name, text, with_photo=with_photo)
    elif command == 'as':
        handle_as_say(bot, chat, job_queue, message, rest, with_photo=with_photo)
    elif command == 'hd':
        handle_roll(message, name, rest, job_queue, hide=True)
    elif command == 'init':
        handle_initiative(message, job_queue, name, rest)
    elif command in ('del', 'delete', 'edit', 'lift'):
        if not chat.recording:
            error_message(message, job_queue, '未在记录中，无法编辑消息')
        elif command == 'del' or command == 'delete':
            handle_delete(message, job_queue)
        elif command == 'edit':
            handle_edit(bot, chat, job_queue, message, rest)
        elif command == 'lift':
            handle_lift(update, job_queue)
    else:
        handle_say(bot, chat, job_queue, message, name, rest, with_photo=with_photo)
    save_username(chat.chat_id, message.from_user.username, name)


def handle_photo(message: telegram.Message):
    photo_size_list = message.photo
    if len(photo_size_list) == 0:
        return None
    photo_size_list.sort(key=lambda p: p.file_size)
    return photo_size_list[-1]


def error(_, update, bot_error):
    """Log Errors caused by Updates."""
    logger.warning('Update "%s" caused error "%s"', update, bot_error)


def handle_status(bot, update):
    assert isinstance(update.message, telegram.Message)
    message = update.message
    chat = get_chat(message.chat)
    if message.new_chat_title:
        chat.title = message.new_chat_title
        chat.save()
    if message.new_chat_members:
        for user in message.new_chat_members:
            if user.id == bot.id:
                message.chat.send_message(
                    START_TEXT,
                    parse_mode='Markdown',
                    disable_web_page_preview=True
                )


def get_chat(telegram_chat: telegram.Chat) -> Chat:
    chat = Chat.objects.filter(
        chat_id=telegram_chat.id
    ).first()
    if chat:
        return chat
    else:
        return Chat.objects.create(
            chat_id=telegram_chat.id,
            title=telegram_chat.title,
        )


def main():
    """Start the bot."""
    # Create the EventHandler and pass it your bot's token.
    updater = Updater(TOKEN)

    # Get the dispatcher to register handlers
    dp = updater.dispatcher

    # on different commands - answer in Telegram
    dp.add_handler(CommandHandler("start", start, pass_job_queue=True))
    dp.add_handler(CommandHandler("save", save, pass_job_queue=True))
    dp.add_handler(CommandHandler("help", bot_help))
    dp.add_handler(CommandHandler('face', set_dice_face, pass_args=True, pass_job_queue=True))
    dp.add_handler(CommandHandler('name', set_name, pass_args=True, pass_job_queue=True))
    dp.add_handler(CommandHandler('round', start_round))

    dp.add_handler(MessageHandler(
        Filters.text | Filters.photo,
        handle_message,
        channel_post_updates=False,
        pass_job_queue=True,
    ))
    dp.add_handler(MessageHandler(Filters.status_update, handle_status))
    # always execute `run_chat_job`.
    dp.add_handler(
        MessageHandler(
            Filters.all,
            run_chat_job,
            channel_post_updates=False,
            pass_job_queue=True,
        ),
        group=42
    )

    updater.dispatcher.add_handler(CallbackQueryHandler(inline_callback))
    # log all errors
    dp.add_error_handler(error)

    # Start the Bot
    if 'WEBHOOK_URL' in os.environ:
        updater.start_webhook(listen='0.0.0.0', port=9990, url_path=TOKEN)
        url = os.path.join(os.environ['WEBHOOK_URL'], TOKEN)
        updater.bot.set_webhook(url=url)
    else:
        updater.start_polling()

    # Run the bot until you press Ctrl-C or the process receives SIGINT,
    # SIGTERM or SIGABRT. This should be used most of the time, since
    # start_polling() is non-blocking and will stop the bot gracefully.
    updater.idle()


if __name__ == '__main__':
    main()
