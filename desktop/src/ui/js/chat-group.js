/* 聊天工具函数:分组管理 */
/* 启动工具分组:把本次回答的工具调用/思考/计划折叠到单个卡片中 */
function _startGroup() {
  if (App._currentGroup) {
    App._currentGroup.wrap.remove();
    App._currentGroup = null;
  }
  if (App.Group && App.Group.start) App.Group.start();
}

/* 结束工具分组:根据是否有答案/工具展示最终摘要 */
function _finishGroup(answerText) {
  if (App.Group && App.Group.finish) {
    App.Group.finish(answerText || '', false);
  }
  App._currentGroup = null;
}
