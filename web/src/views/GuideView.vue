<script setup lang="ts">
import { session } from '../session'

const server = window.location.origin

function print() {
  window.print()
}
</script>

<template>
  <div class="guide">
    <div class="head">
      <h2>Инструкция оператора стенда NAPI-C</h2>
      <span class="spacer" />
      <button class="noprint" @click="print">Печать / PDF</button>
      <router-link v-if="!session.username" class="noprint" to="/login">Войти в стенд</router-link>
    </div>
    <p class="lead">
      Стенд проверяет платы NAPI-C (память, сеть, SD-NAND) и записывает на SD-NAND операционную систему. Всё делается в
      браузере, командная строка не нужна. Адрес стенда: <b class="mono">{{ server }}</b>
    </p>

    <section>
      <h3><span class="n">1</span> Что нужно</h3>
      <ul>
        <li>Компьютер с Windows, balenaEtcher и кардридер.</li>
        <li>Тестовые microSD от 8 ГБ, по одной на каждую плату, которая проверяется одновременно.</li>
        <li>Платы NAPI-C, кабели Ethernet в сеть стенда, блоки питания.</li>
      </ul>
    </section>

    <section>
      <h3><span class="n">2</span> Подготовить тестовые microSD <small>(один раз)</small></h3>
      <ol>
        <li>Откройте стенд в браузере, войдите, вверху выберите <b>«microSD»</b> и нажмите <b>«Скачать образ microSD»</b>.</li>
        <li>Прошейте скачанный файл на карту через balenaEtcher. Распаковывать файл не нужно.</li>
        <li>Сначала проверьте одну карту: вставьте её в плату (шаг 3). Если плата появилась на стенде, прошейте тем же
          файлом остальные карты.</li>
      </ol>
      <p class="hint">
        После обновлений стенда карты перешивать не нужно: программа на них обновляется сама. Перешить карту нужно,
        только если она сломалась или её содержимое испорчено.
      </p>
    </section>

    <section>
      <h3><span class="n">3</span> Подключить платы</h3>
      <ol>
        <li>Вставьте тестовую microSD в плату.</li>
        <li>Подключите Ethernet, затем питание.</li>
        <li>Через 1–2 минуты плата появится на главной странице стенда синим квадратом «готов».</li>
      </ol>
      <p class="hint">
        Новая карта при первом включении дольше готовится: около 3 минут. Затем она скачивает с сервера образы ОС
        (несколько минут). Пока образа нет на карте, стенд не даст запустить прошивку и напишет, каких плат это касается.
      </p>
    </section>

    <section>
      <h3><span class="n">4</span> Запустить проверку</h3>
      <ol>
        <li>В поле <b>«Плат на стенде»</b> введите, сколько плат вы подключили. Стенд покажет, если какая-то не появилась.</li>
        <li>Нажмите <b>«Выбрать все online»</b>.</li>
        <li>В списке <b>«ОС на SD-NAND»</b> оставьте выбранное по умолчанию (Armbian), если не сказано иное.</li>
        <li>Нажмите нужную кнопку:
          <ul>
            <li><b>«Тесты + прошивка»</b> — обычный режим: проверка и запись ОС;</li>
            <li><b>«Только тесты»</b> — проверка без записи;</li>
            <li><b>«Только прошивка»</b> — запись ОС на уже проверенные платы.</li>
          </ul>
        </li>
        <li>Подтвердите в окне. Квадраты станут жёлтыми, полоска под номером показывает, что идёт: RAM, NET, SD, FLASH, CHECKB.</li>
      </ol>
    </section>

    <section>
      <h3><span class="n">5</span> Проверка загрузки (CHECKB)</h3>
      <p>
        После прошивки квадрат станет жёлтым с надписью <b>«ожидание загрузки»</b>. Тогда:
        <b>выключите плату, выньте microSD, включите плату.</b> Плата загрузится с SD-NAND, и стенд сам это увидит
        (до 10 минут). MicroSD больше не вставляйте в эту плату, карта нужна для следующей.
      </p>
      <p class="hint">
        Если проверка загрузки не нужна, перед запуском поставьте галочку «Не проверять загрузку с SD-NAND (BOOTCHECK)».
      </p>
    </section>

    <section>
      <h3><span class="n">6</span> Результат</h3>
      <table class="legend">
        <tbody>
          <tr><td><span class="sw blue" /></td><td><b>Синяя рамка</b></td><td>плата готова, проверка не идёт</td></tr>
          <tr><td><span class="sw yellow" /></td><td><b>Жёлтая</b></td><td>идёт проверка или ожидание загрузки</td></tr>
          <tr><td><span class="sw green" /></td><td><b>Зелёная</b></td><td>годна (PASS). Толстая зелёная — пройдены тесты и прошивка</td></tr>
          <tr><td><span class="sw red" /></td><td><b>Красная</b></td><td>брак (FAIL). Причина написана под полоской и в карточке платы. Отложите плату отдельно</td></tr>
          <tr><td><span class="sw grey" /></td><td><b>Серая</b></td><td>не выполнено или прервано: проблема стенда, <b>не брак</b>. Повторите проверку</td></tr>
          <tr><td><span class="sw dashed" /></td><td><b>Серая пунктирная</b></td><td>нет связи с платой: проверьте питание, Ethernet и microSD</td></tr>
        </tbody>
      </table>
    </section>

    <section>
      <h3><span class="n">7</span> После проверки</h3>
      <ul>
        <li><b>«Экспорт CSV»</b> — отчёт по партии. Все партии — в разделе «История партий».</li>
        <li>Этикетка годной платы: дважды щёлкните по квадрату, в открывшейся карточке платы нажмите «Этикетка».</li>
        <li>Повторить проверку для некоторых плат: выделите их квадраты (один щелчок по квадрату) и нажмите <b>«Повторить для выбранных»</b>.</li>
      </ul>
    </section>

    <section>
      <h3><span class="n">8</span> Если что-то не так</h3>
      <dl>
        <dt>Плата не появилась</dt>
        <dd>Проверьте питание, кабель Ethernet и что microSD вставлена до конца. Подождите 3 минуты. Не помогло —
          перешейте карту (шаг 2) или попробуйте другую.</dd>
        <dt>«Образ ещё не на картах»</dt>
        <dd>Карты докачивают образ ОС между проверками. Подождите несколько минут. Сколько карт готово, видно на
          странице «Образы».</dd>
        <dt>Нужно остановить</dt>
        <dd>Кнопка <b>«Остановить»</b>. Если прошивка была прервана (или пропало питание), запустите для этой платы
          <b>«Только прошивка»</b>.</dd>
        <dt>Много красных подряд или сообщения о стенде</dt>
        <dd>Остановите партию и сообщите инженеру: возможно, неисправен стенд, а не платы.</dd>
      </dl>
    </section>
  </div>
</template>

<style scoped>
.guide {
  max-width: 860px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  font-size: 15px;
  line-height: 1.5;
}
.head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.head h2 {
  margin: 0;
}
.spacer {
  flex: 1;
}
.lead {
  margin: 0;
}
section {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px 18px;
  break-inside: avoid;
}
h3 {
  margin: 0 0 6px;
  font-size: 17px;
  display: flex;
  align-items: center;
  gap: 8px;
}
h3 small {
  font-weight: normal;
  color: var(--muted);
  font-size: 14px;
}
.n {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: var(--blue);
  color: #fff;
  font-size: 14px;
}
ol,
ul {
  margin: 4px 0;
  padding-left: 22px;
}
li {
  margin: 3px 0;
}
p {
  margin: 4px 0;
}
.hint {
  color: var(--muted);
  font-size: 14px;
}
.legend td {
  padding: 4px 8px 4px 0;
  vertical-align: middle;
}
.sw {
  display: inline-block;
  width: 26px;
  height: 18px;
  border-radius: 4px;
  border: 3px solid;
  box-sizing: border-box;
}
.sw.blue {
  border-color: var(--blue);
}
.sw.yellow {
  border-color: var(--yellow);
}
.sw.green {
  border-color: var(--green);
}
.sw.red {
  border-color: var(--red);
}
.sw.grey {
  border-color: #8a8f98;
}
.sw.dashed {
  border: 3px dashed #9aa0a8;
}
dt {
  font-weight: 600;
  margin-top: 6px;
}
dd {
  margin: 2px 0 0 0;
}
@media print {
  .noprint {
    display: none;
  }
  .guide {
    font-size: 12px;
    max-width: none;
  }
  section {
    border: none;
    padding: 4px 0;
  }
}
</style>
