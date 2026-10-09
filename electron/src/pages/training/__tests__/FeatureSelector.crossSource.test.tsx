/**
 * 跨源因子库在**选择器渲染层**的护栏（纯函数层见 trainingCrossSource.test.ts）。
 *
 * 为什么单列一层：合并出来的分类表里，「跨库同名的影子副本」是一个真实存在、
 * 却不可勾选的格子。纯函数只证明它被标了 `disabled`；只有渲染层能证明
 * **点下去真的不会改变勾选**——否则用户点一下副库那份 MOM_5，
 * 后端就在提交时按裸名撞 422（或者更糟：静默换成另一个库的同名因子）。
 * 同理，「全选本类特征」若把影子副本算进本类，取消全选会连带删掉**归属库那一份**
 * 的勾选——用户只是想在副库里取消，锚库的特征却掉了。
 *
 * 断言全部落在用户看得见的文本/标签上（「同名」标、来源库标、计数），
 * 不测组件内部状态——格子怎么算出 disabled 由纯函数测试负责。
 * 右侧「特征预览」也会渲染分类名与已选标签，故查询一律限定在左侧格子内
 * （`.ant-tag` 是预览里的标签，格子里的文案不是；分类标题在 <button> 里）。
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';

import { FeatureSelector } from '../FeatureSelector';
import { mergeSourceFeatureCategories, type FeatureCategory } from '../trainingUtils';

const cat = (id: string, name: string, keys: string[]): FeatureCategory => ({
  id,
  name,
  icon: null,
  features: keys.map((key) => ({ key, label: key })),
});

const ANCHOR = 'l1_factors';
const EXTRA_A = 'l2_factors';

// MOM_5 两边都有：归属锚库（锚库优先），副库那份是影子副本
const ANCHOR_CATS = [cat('momentum', '动量', ['VOLUME48', 'MOM_5'])];
const EXTRA_CATS = [cat('alpha101', 'Alpha101', ['ALPHA_001', 'MOM_5'])];
const LABELS = { [ANCHOR]: 'L1 基础因子', [EXTRA_A]: 'L2 资金流' };

const merged = () => mergeSourceFeatureCategories(ANCHOR, ANCHOR_CATS, { [EXTRA_A]: EXTRA_CATS }, LABELS);

const renderSelector = (selectedFeatures: string[], onChange = vi.fn()) => {
  render(
    <FeatureSelector
      categories={merged()}
      selectedFeatures={selectedFeatures}
      onChange={onChange}
      loading={false}
      anchorSource={ANCHOR}
    />,
  );
  return onChange;
};

/** 展开某个分类（首个分类默认展开，副库那份是收起的）；分类标题是左侧的 <button> */
const expandCategory = async (user: ReturnType<typeof userEvent.setup>, name: string) => {
  const header = screen.getAllByRole('button').find((button) => button.textContent?.includes(name));
  expect(header, `未找到分类标题：${name}`).toBeTruthy();
  await user.click(header as HTMLElement);
};

/** 左侧特征格子（排除右侧预览里的 antd 标签与分类标题按钮） */
const tilesFor = (key: string) =>
  screen
    .getAllByText(key)
    .filter((node) => !node.closest('.ant-tag') && !node.closest('button'));

describe('FeatureSelector 跨源渲染', () => {
  it('副库同名副本标「同名」且不可勾选：点它不会改变勾选', async () => {
    const user = userEvent.setup();
    const onChange = renderSelector(['MOM_5']);
    await expandCategory(user, 'Alpha101 · L2 资金流');

    const tiles = tilesFor('MOM_5');
    expect(tiles).toHaveLength(2);
    expect(screen.getByText('同名')).toBeInTheDocument();

    const blockedTile = tiles[1].closest('[class*="cursor-not-allowed"]');
    expect(blockedTile).toBeTruthy();
    await user.click(blockedTile as HTMLElement);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('归属库那一份仍可勾选：点它会真的改勾选（证明上一条不是「整个格子都点不动」）', async () => {
    const user = userEvent.setup();
    const onChange = renderSelector(['MOM_5']);

    await user.click(tilesFor('MOM_5')[0]);
    expect(onChange).toHaveBeenCalledWith([]);
  });

  it('副库特征带来源库标，用户一眼看出这列不来自锚库', async () => {
    const user = userEvent.setup();
    renderSelector(['ALPHA_001']);
    await expandCategory(user, 'Alpha101 · L2 资金流');

    const extraTiles = tilesFor('ALPHA_001');
    expect(extraTiles).toHaveLength(1);
    // 来源标挂在格子上（副库分类里的同名副本也有，故不按全屏计数断言）
    expect(extraTiles[0].closest('[class*="rounded-2xl"]')?.textContent).toContain('L2 资金流');
    // 锚库特征不标来源，避免同屏堆两层来源标签
    expect(screen.queryByText('L1 基础因子')).not.toBeInTheDocument();
  });

  it('「全选本类特征」不含影子副本：取消全选不会删掉归属库的勾选', async () => {
    const user = userEvent.setup();
    // 副库分类里 ALPHA_001 可勾选、MOM_5（影子副本）不可勾选 → 计数只算 1
    const onChange = renderSelector(['ALPHA_001', 'MOM_5']);
    await expandCategory(user, 'Alpha101 · L2 资金流');

    expect(screen.getByText('全选本类特征 (1)')).toBeInTheDocument();
    await user.click(screen.getByText('全选本类特征 (1)'));

    // MOM_5 归锚库，不在被取消的集合里
    expect(onChange).toHaveBeenCalledWith(['MOM_5']);
  });
});
