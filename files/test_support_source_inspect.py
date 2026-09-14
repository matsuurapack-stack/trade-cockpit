# 2026-09-14新規（Bugfix: isolate global state between test modules）：
#
# `inspect.getsource()`はPythonの`linecache`モジュールが持つ「プロセス全体で共有される
# ファイル内容キャッシュ」に依存する。`linecache`は一度ファイルを読み込むとキャッシュに
# 積んだ行リストをそのまま使い続け、`linecache.checkcache()`／`clearcache()`を明示的に
# 呼ばない限り自動では再検証しない（＝Pythonのドキュメント化された既知の挙動）。
#
# このリポジトリはOneDriveで同期されるフォルダ配下にあり、`investment_db.py`・`server.py`
# （数千行・50万バイト超）はテストスイート全体の実行中（`python -m unittest discover`は
# 数分〜10数分かかる）にOneDriveのバックグラウンド同期がファイルへ触れる可能性がある。
# その最初の読み込みタイミングが同期処理と重なると、`linecache`のキャッシュへ一時的に
# 不整合な行リストが積まれ、以降その回のプロセスが終わるまで、同じファイル内の別関数への
# `inspect.getsource()`呼び出しが軒並み「関数のコード上の行番号(co_firstlineno)は正しいのに
# 中身が数行だけの無関係な断片になる」という壊れ方をする（実際にfull discover実行で複数回
# 再現：test_mu_s2_shared_private_isolation・test_nicosoku_phase9/10/12・
# test_trade_experience_learning・test_fast_quote_qf1の合計13件のソース検査テストが、
# 実行のたびに異なる組み合わせで一時的に失敗していた。単体実行・数ファイルの小規模な
# 組み合わせ実行では毎回100%成功し、実行時間が数分を超える大規模実行でのみ非決定的に
# 再現したことから、特定のテストが特定の順序で他のテストを壊す「テストコード同士の
# 状態汚染」ではなく、Pythonプロセス共有のlinecacheキャッシュが外部要因（OneDrive同期）で
# 一度でも汚染されると尾を引く、という実行環境由来の問題と特定した）。
#
# 対処：`inspect.getsource()`を呼ぶ直前に対象ファイルの`linecache`キャッシュを明示的に
# 破棄してから読み直す。これにより「一度汚染されたら以降ずっと壊れたまま」という状態を
# 「呼び出しのたびに必ず最新のファイル内容を読み直す」へ変える（production codeは一切
# 変更しない、テスト側だけの対処）。

import inspect
import linecache


def get_fresh_source(obj):
    """inspect.getsource(obj)と同じ結果を返すが、呼び出し直前にlinecacheの該当ファイルの
    キャッシュエントリを明示的に捨ててから読み直す。プロセス全体で共有されるlinecache
    キャッシュが他のテスト実行中（あるいはOneDrive等の外部同期）によって一時的に汚染
    されていても、このヘルパー経由なら常にディスク上の最新内容を反映したソースを取得
    できる。linecache.checkcache()はmtime/サイズが前回と同じなら再読込をスキップして
    しまう（=汚染時のmtimeと現在のmtimeがたまたま一致していると効かない）ため、
    checkcacheではなくキャッシュエントリ自体をpopして無条件に再読込させる。"""
    try:
        filename = inspect.getsourcefile(obj) or inspect.getfile(obj)
    except TypeError:
        filename = None
    if filename:
        linecache.cache.pop(filename, None)
    return inspect.getsource(obj)
