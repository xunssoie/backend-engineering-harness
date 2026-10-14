// 동시성 폭발 부하 스크립트 템플릿.
// {…} 자리를 채워 .claude/resources/concurrency/{이슈번호}/{슬러그}/burst-script.js 로 저장한다.
//
// 실행: measure.sh가 부른다. 직접 돌릴 때는
//   k6 run -e SUMMARY_OUT=$TARGET_DIR/k6-burst-raw-{n}.json -e BASE_URL={perf.appBaseUrl} $TARGET_DIR/burst-script.js
//
// {n}은 후보 번호다. 0 = 아무 제어도 없는 원본, n = 후보 n을 단독 적용한 상태.
// 실행은 호출자가 한다. 스킬은 명령어만 제시한다.
//
// ── 작성 규칙 ──────────────────────────────────────────────
// 1. 이 파일을 복사해 고친다. 빈 파일에서 새로 쓰지 않는다.
// 2. 고치는 자리는 META 블록, EXPECTED_REJECT_*, default 함수의 요청 한 줄뿐이다.
// 3. **executor는 per-vu-iterations, iterations: 1을 유지한다.**
//    VU마다 요청을 정확히 한 번 보내 "N명이 동시에 버튼을 누른다"를 만든다.
//    ramping-vus로 바꾸지 마라. 램프를 두면 경합이 시간축으로 흩어져 결함이 재현되지 않는다.
//    gracefulStop을 늘리지 마라. 요청이 끝나기를 기다리는 시간일 뿐 경합과 무관하다.
// 4. **토큰은 __VU로 고른다.** VU와 사용자가 1대1로 묶여야 같은 사용자의 중복 요청이 섞이지 않는다.
//    optimize-performance 템플릿의 exec.scenario.iterationInTest 방식을 가져오지 마라.
//    그 방식은 VU 수와 무관하게 토큰을 순환시키므로 여기서는 같은 사용자가 두 번 요청하게 된다.
// 5. **토큰 수와 VU 수가 다르면 중단한다.** 하나라도 어긋나면 위반 건수가 왜곡된다.
// 6. 인증 헤더 이름과 접두어는 measure.sh가 perf.tokens.header, headerPrefix를 넘긴다.
//    여기서 하드코딩하지 마라. 서버가 읽는 헤더와 다르면 전 요청이 401로 끝난다.
// 7. **thresholds를 두지 마라.** 실패율이 높은 것이 정상인 측정이다.
//    상한 100에 VU 500이면 400건은 반드시 거절된다. threshold를 걸면 k6가 실패로 끝낸다.
// 8. 응답 코드와 에러 코드를 모두 센다. 정합성이 지켜져도 거절이 500으로 나가면 결함이다.
//    EXPECTED_REJECT_CODE에는 상한 초과 시 기대하는 에러 코드를, ERROR_CODE_FIELD에는 응답 본문에서
//    그 코드가 담긴 필드명을 적는다. 에러 코드가 없는 서버면 EXPECTED_REJECT_CODE를 null로 두고
//    EXPECTED_REJECT_STATUS만으로 센다.
// 9. **워밍업 시나리오를 만들지 마라.** 폭발은 한 번만 터뜨린다.
//    워밍업이 상한을 채워버리면 본 측정에서 전 요청이 거절된다.
// 10. 되돌리기는 스크립트가 하지 않는다. measure.sh가 부하 직전에 reset.sql로 처리한다.
//     setup()이나 teardown()에 DB 정리를 넣지 마라. k6는 DB에 접속하지 않는다.
// 11. **handleSummary는 고치지 마라.** k6 원본 요약에 META만 얹어 그대로 쓴다.
//     가공(응답 분포, 지연 분위수, delta_vs_base)은 harness conc-burst-summary가 한다.
// ──────────────────────────────────────────────────────────

import http from 'k6/http';
import { Counter } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';
const AUTH_HEADER = __ENV.AUTH_HEADER || 'Authorization';
const AUTH_PREFIX = __ENV.AUTH_PREFIX === undefined ? 'Bearer ' : __ENV.AUTH_PREFIX;

// 이 측정이 무엇이었는지 요약 파일만 보고 알 수 있게 한다. 후보마다 바꾸지 마라.
const META = {
    target: '{슬러그}',
    endpoint: '{HTTP 메서드} {경로}',
    executor: 'per-vu-iterations (iterations=1, ramp 없음)',
    resource_id: {다투는 자원 id},
    capacity: {허용 상한},
    vus: {VU 수},
    expected_reject_status: {상한 초과 시 기대 상태 코드},
    expected_reject_code: {상한 초과 시 기대 에러 코드 또는 null},
    db_cache: 'warm',
    app_cache: '{없음 또는 사용 중인 캐시}',
};

const ERROR_CODE_FIELD = 'code';

const tokens = JSON.parse(open(__ENV.TOKENS || '../tokens.json'));

if (tokens.length !== META.vus) {
    throw new Error(
        `토큰 ${tokens.length}건 / 필요 ${META.vus}건. VU와 사용자가 1대1로 묶이지 않는다. ` +
        'mint-tokens.sh의 --count와 시드 사용자 수를 VU 수에 맞추고 다시 만들어라.'
    );
}

const succeeded = new Counter('req_succeeded');
const rejectedAsExpected = new Counter('req_rejected_expected');
const rejectedOther4xx = new Counter('req_rejected_other_4xx');
const failed5xx = new Counter('req_failed_5xx');
const unparsable = new Counter('req_unparsable');

export const options = {
    scenarios: {
        burst: {
            executor: 'per-vu-iterations',
            vus: META.vus,
            iterations: 1,
            // 전 VU가 요청 하나씩만 보내므로 길게 잡을 이유가 없다.
            // 여기에 걸리면 경합이 아니라 락 대기 타임아웃을 재고 있는 것이다.
            maxDuration: '60s',
        },
    },
    summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
    // thresholds 없음. 거절이 대량으로 나는 것이 정상이다. (작성 규칙 7)
};

function tokenOf(t) {
    return typeof t === 'string' ? t : t.accessToken;
}

// 비JSON 응답에서 예외를 던지지 않는다. 파싱 실패는 null로 떨어뜨려 카운터로 드러낸다.
function errorCodeOf(res) {
    if (res.body === null || res.body.length <= 2) {
        return null;
    }
    try {
        const body = res.json();
        return body && body[ERROR_CODE_FIELD] !== undefined ? body[ERROR_CODE_FIELD] : null;
    } catch (e) {
        return null;
    }
}

export default function () {
    // VU 번호로 사용자를 고정한다. __VU는 1부터 시작한다.
    const token = tokenOf(tokens[__VU - 1]);

    const params = {
        headers: { [AUTH_HEADER]: `${AUTH_PREFIX}${token}` },
    };

    // 요청 한 줄. 메서드와 경로, 본문을 대상에 맞게 고친다.
    const res = http.request('{HTTP 메서드}', `${BASE_URL}{경로}`, null, params);

    if (res.status >= 200 && res.status < 300) {
        succeeded.add(1);
        return;
    }

    if (res.status >= 500) {
        failed5xx.add(1);
        return;
    }

    if (META.expected_reject_code === null) {
        (res.status === META.expected_reject_status ? rejectedAsExpected : rejectedOther4xx).add(1);
        return;
    }

    const code = errorCodeOf(res);

    if (code === null) {
        unparsable.add(1);
        return;
    }

    if (res.status === META.expected_reject_status && code === META.expected_reject_code) {
        rejectedAsExpected.add(1);
        return;
    }

    rejectedOther4xx.add(1);
}

export function handleSummary(data) {
    const m = data.metrics || {};
    const count = (name) => (m[name] && m[name].values.count) || 0;
    const p99 = m.http_req_duration ? m.http_req_duration.values['p(99)'] : null;

    // 터미널 한 줄 요약. 반올림은 여기에만 쓴다. 파일에는 원본을 그대로 쓴다.
    const line = [
        `[burst] ${META.target}`,
        `성공 ${count('req_succeeded')} (상한 ${META.capacity})`,
        `기대거절 ${count('req_rejected_expected')}`,
        `기타4xx ${count('req_rejected_other_4xx')}`,
        `5xx ${count('req_failed_5xx')}`,
        `파싱불가 ${count('req_unparsable')}`,
        `p99 ${typeof p99 === 'number' ? p99.toFixed(1) : '-'}ms`,
    ].join(' / ');

    const out = { stdout: `\n${line}\n\n` };

    if (__ENV.SUMMARY_OUT) {
        out[__ENV.SUMMARY_OUT] = JSON.stringify(Object.assign({ burst_meta: META }, data));
    }

    return out;
}
