# Curated success wrist-view static-check report

Input: `/home/ubuntu/data/curated_samples/success`

## Main Rule

`left_wrist_view` and `right_wrist_view` are judged independently.

- `non-static`: reliable global self-motion evidence, or repeated strong dominant/local motion when the view is dominated by low-texture/black foreground.
- `static`: no reliable self-motion evidence was found.

## Summary

- Wrist-view videos checked: 1728
- Static views: 233
- Non-static views: 1495
- Samples for reference: 1000
- Samples with any non-static view: 940
- Samples whose existing views are all static: 60

## Non-static Views

| sample | view | verdict | moved/valid | strong-local | path |
| --- | --- | --- | --- | --- | --- |
| add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-04/umi/yangwenzhe/episode_0018 | right_wrist_view | non-static | 6/19 | 3 | /home/ubuntu/data/curated_samples/success/add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-04/umi/yangwenzhe/episode_0018 |
| add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-05/umi/wuhao/episode_0038 | left_wrist_view | non-static | 14/20 | 3 | /home/ubuntu/data/curated_samples/success/add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-05/umi/wuhao/episode_0038 |
| add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-05/umi/wuhao/episode_0038 | right_wrist_view | non-static | 10/16 | 3 | /home/ubuntu/data/curated_samples/success/add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-05/umi/wuhao/episode_0038 |
| add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-05/umi/wuhao/episode_0057 | left_wrist_view | non-static | 10/18 | 3 | /home/ubuntu/data/curated_samples/success/add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-05/umi/wuhao/episode_0057 |
| add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-05/umi/wuhao/episode_0057 | right_wrist_view | non-static | 10/11 | 6 | /home/ubuntu/data/curated_samples/success/add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-05/umi/wuhao/episode_0057 |
| add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-05/umi/wuhao/episode_0072 | left_wrist_view | non-static | 9/20 | 3 | /home/ubuntu/data/curated_samples/success/add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-05/umi/wuhao/episode_0072 |
| add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-05/umi/wuhao/episode_0072 | right_wrist_view | non-static | 9/15 | 3 | /home/ubuntu/data/curated_samples/success/add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-05/umi/wuhao/episode_0072 |
| add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-06/umi/wuhuming/episode_0042 | left_wrist_view | non-static | 5/19 | 3 | /home/ubuntu/data/curated_samples/success/add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-06/umi/wuhuming/episode_0042 |
| add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-06/umi/wuhuming/episode_0042 | right_wrist_view | non-static | 5/20 | 1 | /home/ubuntu/data/curated_samples/success/add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-06/umi/wuhuming/episode_0042 |
| add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-08/umi/lihaoran/episode_0010 | left_wrist_view | non-static | 5/23 | 0 | /home/ubuntu/data/curated_samples/success/add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-08/umi/lihaoran/episode_0010 |
| add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-08/umi/lihaoran/episode_0010 | right_wrist_view | non-static | 16/20 | 2 | /home/ubuntu/data/curated_samples/success/add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-08/umi/lihaoran/episode_0010 |
| add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-10/umi/liuguanjun/episode_0002 | right_wrist_view | non-static | 8/23 | 1 | /home/ubuntu/data/curated_samples/success/add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-10/umi/liuguanjun/episode_0002 |
| add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-16/umi/wangpinye/episode_0037 | right_wrist_view | non-static | 4/11 | 7 | /home/ubuntu/data/curated_samples/success/add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-16/umi/wangpinye/episode_0037 |
| assemble_battery/2026-06-03/umi/yuanhao/episode_0059 | left_wrist_view | non-static | 11/18 | 4 | /home/ubuntu/data/curated_samples/success/assemble_battery/2026-06-03/umi/yuanhao/episode_0059 |
| assemble_battery/2026-06-03/umi/yuanhao/episode_0059 | right_wrist_view | non-static | 5/21 | 0 | /home/ubuntu/data/curated_samples/success/assemble_battery/2026-06-03/umi/yuanhao/episode_0059 |
| assemble_battery/2026-06-07/umi/shixuewen/episode_0005 | right_wrist_view | non-static | 11/20 | 2 | /home/ubuntu/data/curated_samples/success/assemble_battery/2026-06-07/umi/shixuewen/episode_0005 |
| assemble_battery/2026-06-08/umi/wuhuming/episode_0088 | left_wrist_view | non-static | 6/17 | 3 | /home/ubuntu/data/curated_samples/success/assemble_battery/2026-06-08/umi/wuhuming/episode_0088 |
| assemble_battery/2026-06-08/umi/wuhuming/episode_0088 | right_wrist_view | non-static | 2/11 | 8 | /home/ubuntu/data/curated_samples/success/assemble_battery/2026-06-08/umi/wuhuming/episode_0088 |
| assemble_battery/2026-06-09/umi/tianchangwu/episode_0010 | left_wrist_view | non-static | 10/15 | 6 | /home/ubuntu/data/curated_samples/success/assemble_battery/2026-06-09/umi/tianchangwu/episode_0010 |
| assemble_battery/2026-06-10/umi/liuguanjun/episode_0010 | left_wrist_view | non-static | 5/14 | 7 | /home/ubuntu/data/curated_samples/success/assemble_battery/2026-06-10/umi/liuguanjun/episode_0010 |
| assemble_battery/2026-06-10/umi/liuguanjun/episode_0010 | right_wrist_view | non-static | 12/17 | 2 | /home/ubuntu/data/curated_samples/success/assemble_battery/2026-06-10/umi/liuguanjun/episode_0010 |
| assemble_battery/2026-06-16/umi/yanqiang/episode_0125 | left_wrist_view | non-static | 4/18 | 4 | /home/ubuntu/data/curated_samples/success/assemble_battery/2026-06-16/umi/yanqiang/episode_0125 |
| assemble_battery/2026-06-16/umi/yanqiang/episode_0125 | right_wrist_view | non-static | 7/21 | 0 | /home/ubuntu/data/curated_samples/success/assemble_battery/2026-06-16/umi/yanqiang/episode_0125 |
| assemble_the_remote_controller_put_into_drawer/2026-05-27/umi/penglizhuan/episode_0061 | left_wrist_view | non-static | 10/22 | 0 | /home/ubuntu/data/curated_samples/success/assemble_the_remote_controller_put_into_drawer/2026-05-27/umi/penglizhuan/episode_0061 |
| assemble_the_remote_controller_put_into_drawer/2026-05-27/umi/penglizhuan/episode_0061 | right_wrist_view | non-static | 10/19 | 1 | /home/ubuntu/data/curated_samples/success/assemble_the_remote_controller_put_into_drawer/2026-05-27/umi/penglizhuan/episode_0061 |
| assemble_the_remote_controller_put_into_drawer/2026-05-27/umi/penglizhuan/episode_0064 | left_wrist_view | non-static | 8/19 | 0 | /home/ubuntu/data/curated_samples/success/assemble_the_remote_controller_put_into_drawer/2026-05-27/umi/penglizhuan/episode_0064 |
| assemble_the_remote_controller_put_into_drawer/2026-05-27/umi/penglizhuan/episode_0064 | right_wrist_view | non-static | 7/16 | 1 | /home/ubuntu/data/curated_samples/success/assemble_the_remote_controller_put_into_drawer/2026-05-27/umi/penglizhuan/episode_0064 |
| assemble_the_remote_controller_put_into_drawer/2026-05-27/umi/penglizhuan/episode_0078 | left_wrist_view | non-static | 5/19 | 1 | /home/ubuntu/data/curated_samples/success/assemble_the_remote_controller_put_into_drawer/2026-05-27/umi/penglizhuan/episode_0078 |
| assemble_the_remote_controller_put_into_drawer/2026-05-27/umi/penglizhuan/episode_0078 | right_wrist_view | non-static | 4/21 | 0 | /home/ubuntu/data/curated_samples/success/assemble_the_remote_controller_put_into_drawer/2026-05-27/umi/penglizhuan/episode_0078 |
| assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0017 | right_wrist_view | non-static | 5/17 | 4 | /home/ubuntu/data/curated_samples/success/assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0017 |
| assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0080 | left_wrist_view | non-static | 5/18 | 5 | /home/ubuntu/data/curated_samples/success/assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0080 |
| assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0080 | right_wrist_view | non-static | 5/16 | 5 | /home/ubuntu/data/curated_samples/success/assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0080 |
| assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0093 | left_wrist_view | non-static | 7/20 | 2 | /home/ubuntu/data/curated_samples/success/assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0093 |
| assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0093 | right_wrist_view | non-static | 5/18 | 4 | /home/ubuntu/data/curated_samples/success/assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0093 |
| assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0103 | left_wrist_view | non-static | 5/21 | 3 | /home/ubuntu/data/curated_samples/success/assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0103 |
| assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0103 | right_wrist_view | non-static | 7/19 | 4 | /home/ubuntu/data/curated_samples/success/assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0103 |
| assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0104 | left_wrist_view | non-static | 4/19 | 4 | /home/ubuntu/data/curated_samples/success/assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0104 |
| assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0104 | right_wrist_view | non-static | 8/19 | 4 | /home/ubuntu/data/curated_samples/success/assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0104 |
| bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0002 | left_wrist_view | non-static | 8/12 | 5 | /home/ubuntu/data/curated_samples/success/bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0002 |
| bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0002 | right_wrist_view | non-static | 5/10 | 8 | /home/ubuntu/data/curated_samples/success/bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0002 |
| bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0004 | left_wrist_view | non-static | 9/13 | 5 | /home/ubuntu/data/curated_samples/success/bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0004 |
| bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0004 | right_wrist_view | non-static | 1/7 | 5 | /home/ubuntu/data/curated_samples/success/bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0004 |
| bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0021 | left_wrist_view | non-static | 4/8 | 6 | /home/ubuntu/data/curated_samples/success/bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0021 |
| bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0021 | right_wrist_view | non-static | 5/16 | 6 | /home/ubuntu/data/curated_samples/success/bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0021 |
| bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0028 | left_wrist_view | non-static | 8/14 | 8 | /home/ubuntu/data/curated_samples/success/bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0028 |
| bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0028 | right_wrist_view | non-static | 8/14 | 5 | /home/ubuntu/data/curated_samples/success/bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0028 |
| bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0083 | left_wrist_view | non-static | 8/11 | 7 | /home/ubuntu/data/curated_samples/success/bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0083 |
| bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0083 | right_wrist_view | non-static | 5/13 | 6 | /home/ubuntu/data/curated_samples/success/bag_covers_the_box/2026-06-13/umi/lipengfei/episode_0083 |
| bag_covers_the_box/2026-06-14/umi/tianchangwu/episode_0000 | left_wrist_view | non-static | 10/16 | 2 | /home/ubuntu/data/curated_samples/success/bag_covers_the_box/2026-06-14/umi/tianchangwu/episode_0000 |
| bag_covers_the_box/2026-06-14/umi/tianchangwu/episode_0000 | right_wrist_view | non-static | 5/11 | 9 | /home/ubuntu/data/curated_samples/success/bag_covers_the_box/2026-06-14/umi/tianchangwu/episode_0000 |
| bag_covers_the_box/2026-06-14/umi/tianchangwu/episode_0014 | left_wrist_view | non-static | 3/9 | 6 | /home/ubuntu/data/curated_samples/success/bag_covers_the_box/2026-06-14/umi/tianchangwu/episode_0014 |
| bag_covers_the_box/2026-06-14/umi/tianchangwu/episode_0014 | right_wrist_view | non-static | 4/10 | 8 | /home/ubuntu/data/curated_samples/success/bag_covers_the_box/2026-06-14/umi/tianchangwu/episode_0014 |
| bag_covers_the_box/2026-06-14/umi/tianchangwu/episode_0051 | left_wrist_view | non-static | 4/13 | 7 | /home/ubuntu/data/curated_samples/success/bag_covers_the_box/2026-06-14/umi/tianchangwu/episode_0051 |
| bag_covers_the_box/2026-06-14/umi/tianchangwu/episode_0051 | right_wrist_view | non-static | 6/14 | 6 | /home/ubuntu/data/curated_samples/success/bag_covers_the_box/2026-06-14/umi/tianchangwu/episode_0051 |
| bag_the_fruit/2026-05-25/umi/tianchangwu/episode_0084 | left_wrist_view | non-static | 6/15 | 4 | /home/ubuntu/data/curated_samples/success/bag_the_fruit/2026-05-25/umi/tianchangwu/episode_0084 |
| bag_the_fruit/2026-05-25/umi/tianchangwu/episode_0084 | right_wrist_view | non-static | 4/16 | 4 | /home/ubuntu/data/curated_samples/success/bag_the_fruit/2026-05-25/umi/tianchangwu/episode_0084 |
| bag_the_fruit/2026-05-29/umi/bianyuchen/episode_0051 | left_wrist_view | non-static | 11/18 | 5 | /home/ubuntu/data/curated_samples/success/bag_the_fruit/2026-05-29/umi/bianyuchen/episode_0051 |
| bag_the_fruit/2026-05-29/umi/bianyuchen/episode_0051 | right_wrist_view | non-static | 6/14 | 10 | /home/ubuntu/data/curated_samples/success/bag_the_fruit/2026-05-29/umi/bianyuchen/episode_0051 |
| bag_the_fruit/2026-06-04/umi/pengshasha/episode_0054 | left_wrist_view | non-static | 7/19 | 4 | /home/ubuntu/data/curated_samples/success/bag_the_fruit/2026-06-04/umi/pengshasha/episode_0054 |
| bag_the_fruit/2026-06-04/umi/pengshasha/episode_0054 | right_wrist_view | non-static | 3/14 | 6 | /home/ubuntu/data/curated_samples/success/bag_the_fruit/2026-06-04/umi/pengshasha/episode_0054 |
| bag_the_fruit/2026-06-11/umi/liuguanjun/episode_0050 | left_wrist_view | non-static | 5/18 | 2 | /home/ubuntu/data/curated_samples/success/bag_the_fruit/2026-06-11/umi/liuguanjun/episode_0050 |
| bag_the_fruit/2026-06-11/umi/liuguanjun/episode_0056 | left_wrist_view | non-static | 4/18 | 1 | /home/ubuntu/data/curated_samples/success/bag_the_fruit/2026-06-11/umi/liuguanjun/episode_0056 |
| bag_the_fruit/2026-06-12/umi/chenjiankang/episode_0041 | right_wrist_view | non-static | 13/19 | 3 | /home/ubuntu/data/curated_samples/success/bag_the_fruit/2026-06-12/umi/chenjiankang/episode_0041 |
| bag_the_fruit/2026-06-15/umi/panlei/episode_0038 | right_wrist_view | non-static | 8/22 | 1 | /home/ubuntu/data/curated_samples/success/bag_the_fruit/2026-06-15/umi/panlei/episode_0038 |
| bag_the_fruit/2026-06-16/umi/wangpinye/episode_0060 | left_wrist_view | non-static | 4/17 | 5 | /home/ubuntu/data/curated_samples/success/bag_the_fruit/2026-06-16/umi/wangpinye/episode_0060 |
| bag_the_shirts/2026-05-26/umi/luochao/episode_0097 | left_wrist_view | non-static | 3/13 | 7 | /home/ubuntu/data/curated_samples/success/bag_the_shirts/2026-05-26/umi/luochao/episode_0097 |
| bag_the_shirts/2026-05-26/umi/luochao/episode_0097 | right_wrist_view | non-static | 5/8 | 5 | /home/ubuntu/data/curated_samples/success/bag_the_shirts/2026-05-26/umi/luochao/episode_0097 |
| bag_the_shirts/2026-06-09/umi/panlei/episode_0043 | left_wrist_view | non-static | 6/15 | 4 | /home/ubuntu/data/curated_samples/success/bag_the_shirts/2026-06-09/umi/panlei/episode_0043 |
| bag_the_shirts/2026-06-09/umi/panlei/episode_0043 | right_wrist_view | non-static | 1/5 | 7 | /home/ubuntu/data/curated_samples/success/bag_the_shirts/2026-06-09/umi/panlei/episode_0043 |
| bag_the_shirts/2026-06-09/umi/panlei/episode_0087 | left_wrist_view | non-static | 7/16 | 1 | /home/ubuntu/data/curated_samples/success/bag_the_shirts/2026-06-09/umi/panlei/episode_0087 |
| bag_the_shirts/2026-06-09/umi/panlei/episode_0087 | right_wrist_view | non-static | 5/8 | 6 | /home/ubuntu/data/curated_samples/success/bag_the_shirts/2026-06-09/umi/panlei/episode_0087 |
| bag_the_shirts/2026-06-10/umi/wangliang/episode_0012 | left_wrist_view | non-static | 4/11 | 11 | /home/ubuntu/data/curated_samples/success/bag_the_shirts/2026-06-10/umi/wangliang/episode_0012 |
| bag_the_shirts/2026-06-10/umi/wangliang/episode_0012 | right_wrist_view | non-static | 2/11 | 7 | /home/ubuntu/data/curated_samples/success/bag_the_shirts/2026-06-10/umi/wangliang/episode_0012 |
| bag_the_shirts/2026-06-12/umi/wangbo/episode_0014 | left_wrist_view | non-static | 5/10 | 5 | /home/ubuntu/data/curated_samples/success/bag_the_shirts/2026-06-12/umi/wangbo/episode_0014 |
| bag_the_shirts/2026-06-12/umi/wangbo/episode_0014 | right_wrist_view | non-static | 2/9 | 9 | /home/ubuntu/data/curated_samples/success/bag_the_shirts/2026-06-12/umi/wangbo/episode_0014 |
| bag_the_shirts/2026-06-12/umi/wangbo/episode_0065 | left_wrist_view | non-static | 3/10 | 4 | /home/ubuntu/data/curated_samples/success/bag_the_shirts/2026-06-12/umi/wangbo/episode_0065 |
| bag_the_shirts/2026-06-12/umi/wangbo/episode_0065 | right_wrist_view | non-static | 3/9 | 10 | /home/ubuntu/data/curated_samples/success/bag_the_shirts/2026-06-12/umi/wangbo/episode_0065 |
| bag_the_shirts/2026-06-12/umi/wangbo/episode_0080 | left_wrist_view | non-static | 3/8 | 7 | /home/ubuntu/data/curated_samples/success/bag_the_shirts/2026-06-12/umi/wangbo/episode_0080 |
| bag_the_shirts/2026-06-12/umi/wangbo/episode_0080 | right_wrist_view | non-static | 3/6 | 12 | /home/ubuntu/data/curated_samples/success/bag_the_shirts/2026-06-12/umi/wangbo/episode_0080 |
| bag_the_shirts/2026-06-13/umi/chenxu/episode_0003 | left_wrist_view | non-static | 3/9 | 5 | /home/ubuntu/data/curated_samples/success/bag_the_shirts/2026-06-13/umi/chenxu/episode_0003 |
| bag_the_shirts/2026-06-13/umi/chenxu/episode_0003 | right_wrist_view | non-static | 6/13 | 4 | /home/ubuntu/data/curated_samples/success/bag_the_shirts/2026-06-13/umi/chenxu/episode_0003 |
| bind_and_secure_the_socks/2026-06-02/umi/liuguanjun/episode_0007 | left_wrist_view | non-static | 9/15 | 3 | /home/ubuntu/data/curated_samples/success/bind_and_secure_the_socks/2026-06-02/umi/liuguanjun/episode_0007 |
| bind_and_secure_the_socks/2026-06-02/umi/liuguanjun/episode_0007 | right_wrist_view | non-static | 1/8 | 6 | /home/ubuntu/data/curated_samples/success/bind_and_secure_the_socks/2026-06-02/umi/liuguanjun/episode_0007 |
| bind_and_secure_the_socks/2026-06-04/umi/wangliang/episode_0029 | left_wrist_view | non-static | 3/14 | 5 | /home/ubuntu/data/curated_samples/success/bind_and_secure_the_socks/2026-06-04/umi/wangliang/episode_0029 |
| bind_and_secure_the_socks/2026-06-04/umi/wangliang/episode_0029 | right_wrist_view | non-static | 7/14 | 7 | /home/ubuntu/data/curated_samples/success/bind_and_secure_the_socks/2026-06-04/umi/wangliang/episode_0029 |
| bind_and_secure_the_socks/2026-06-11/umi/wangbo/episode_0025 | left_wrist_view | non-static | 7/15 | 5 | /home/ubuntu/data/curated_samples/success/bind_and_secure_the_socks/2026-06-11/umi/wangbo/episode_0025 |
| bind_and_secure_the_socks/2026-06-11/umi/wangbo/episode_0025 | right_wrist_view | non-static | 3/9 | 8 | /home/ubuntu/data/curated_samples/success/bind_and_secure_the_socks/2026-06-11/umi/wangbo/episode_0025 |
| bind_and_secure_the_socks/2026-06-12/umi/likun/episode_0002 | left_wrist_view | non-static | 4/18 | 2 | /home/ubuntu/data/curated_samples/success/bind_and_secure_the_socks/2026-06-12/umi/likun/episode_0002 |
| bind_and_secure_the_socks/2026-06-12/umi/likun/episode_0002 | right_wrist_view | non-static | 5/13 | 7 | /home/ubuntu/data/curated_samples/success/bind_and_secure_the_socks/2026-06-12/umi/likun/episode_0002 |
| bind_and_secure_the_socks/2026-06-12/umi/likun/episode_0042 | left_wrist_view | non-static | 7/15 | 3 | /home/ubuntu/data/curated_samples/success/bind_and_secure_the_socks/2026-06-12/umi/likun/episode_0042 |
| bind_and_secure_the_socks/2026-06-12/umi/likun/episode_0042 | right_wrist_view | non-static | 6/18 | 1 | /home/ubuntu/data/curated_samples/success/bind_and_secure_the_socks/2026-06-12/umi/likun/episode_0042 |
| bind_and_secure_the_socks/2026-06-12/umi/likun/episode_0057 | left_wrist_view | non-static | 6/17 | 4 | /home/ubuntu/data/curated_samples/success/bind_and_secure_the_socks/2026-06-12/umi/likun/episode_0057 |
| bind_and_secure_the_socks/2026-06-12/umi/likun/episode_0057 | right_wrist_view | non-static | 7/20 | 1 | /home/ubuntu/data/curated_samples/success/bind_and_secure_the_socks/2026-06-12/umi/likun/episode_0057 |
| bind_and_secure_the_socks/2026-06-15/umi/lihaoran/episode_0050 | left_wrist_view | non-static | 5/13 | 5 | /home/ubuntu/data/curated_samples/success/bind_and_secure_the_socks/2026-06-15/umi/lihaoran/episode_0050 |
| bind_and_secure_the_socks/2026-06-15/umi/lihaoran/episode_0056 | right_wrist_view | non-static | 3/9 | 10 | /home/ubuntu/data/curated_samples/success/bind_and_secure_the_socks/2026-06-15/umi/lihaoran/episode_0056 |
| binding_socks/2026-05-26/umi/wuhao/episode_0004 | left_wrist_view | non-static | 0/11 | 8 | /home/ubuntu/data/curated_samples/success/binding_socks/2026-05-26/umi/wuhao/episode_0004 |
| binding_socks/2026-05-26/umi/wuhao/episode_0004 | right_wrist_view | non-static | 3/9 | 8 | /home/ubuntu/data/curated_samples/success/binding_socks/2026-05-26/umi/wuhao/episode_0004 |
| binding_socks/2026-05-26/umi/wuhao/episode_0008 | left_wrist_view | non-static | 1/13 | 5 | /home/ubuntu/data/curated_samples/success/binding_socks/2026-05-26/umi/wuhao/episode_0008 |
| binding_socks/2026-05-26/umi/wuhao/episode_0008 | right_wrist_view | non-static | 1/9 | 9 | /home/ubuntu/data/curated_samples/success/binding_socks/2026-05-26/umi/wuhao/episode_0008 |
| binding_socks/2026-05-26/umi/wuhao/episode_0048 | left_wrist_view | non-static | 1/12 | 8 | /home/ubuntu/data/curated_samples/success/binding_socks/2026-05-26/umi/wuhao/episode_0048 |
| binding_socks/2026-05-26/umi/wuhao/episode_0048 | right_wrist_view | non-static | 1/7 | 10 | /home/ubuntu/data/curated_samples/success/binding_socks/2026-05-26/umi/wuhao/episode_0048 |
| binding_socks/2026-05-26/umi/wuhao/episode_0050 | left_wrist_view | non-static | 2/12 | 9 | /home/ubuntu/data/curated_samples/success/binding_socks/2026-05-26/umi/wuhao/episode_0050 |
| binding_socks/2026-05-26/umi/wuhao/episode_0050 | right_wrist_view | non-static | 5/10 | 9 | /home/ubuntu/data/curated_samples/success/binding_socks/2026-05-26/umi/wuhao/episode_0050 |
| binding_socks/2026-05-27/umi/lihaoran/episode_0002 | left_wrist_view | non-static | 11/20 | 3 | /home/ubuntu/data/curated_samples/success/binding_socks/2026-05-27/umi/lihaoran/episode_0002 |
| binding_socks/2026-05-27/umi/lihaoran/episode_0002 | right_wrist_view | non-static | 6/12 | 4 | /home/ubuntu/data/curated_samples/success/binding_socks/2026-05-27/umi/lihaoran/episode_0002 |
| binding_socks/2026-05-27/umi/lihaoran/episode_0066 | left_wrist_view | non-static | 7/21 | 1 | /home/ubuntu/data/curated_samples/success/binding_socks/2026-05-27/umi/lihaoran/episode_0066 |
| binding_socks/2026-05-27/umi/lihaoran/episode_0066 | right_wrist_view | non-static | 6/11 | 7 | /home/ubuntu/data/curated_samples/success/binding_socks/2026-05-27/umi/lihaoran/episode_0066 |
| binding_socks/2026-05-28/umi/panlei/episode_0005 | left_wrist_view | non-static | 4/17 | 0 | /home/ubuntu/data/curated_samples/success/binding_socks/2026-05-28/umi/panlei/episode_0005 |
| binding_socks/2026-05-28/umi/panlei/episode_0005 | right_wrist_view | non-static | 4/14 | 3 | /home/ubuntu/data/curated_samples/success/binding_socks/2026-05-28/umi/panlei/episode_0005 |
| binding_socks/2026-05-28/umi/panlei/episode_0030 | left_wrist_view | non-static | 7/22 | 2 | /home/ubuntu/data/curated_samples/success/binding_socks/2026-05-28/umi/panlei/episode_0030 |
| binding_socks/2026-05-28/umi/panlei/episode_0030 | right_wrist_view | non-static | 4/10 | 7 | /home/ubuntu/data/curated_samples/success/binding_socks/2026-05-28/umi/panlei/episode_0030 |
| brush_the_white_plastic_board/2026-06-04/umi/yuanhao/episode_0009 | left_wrist_view | non-static | 5/21 | 2 | /home/ubuntu/data/curated_samples/success/brush_the_white_plastic_board/2026-06-04/umi/yuanhao/episode_0009 |
| brush_the_white_plastic_board/2026-06-04/umi/yuanhao/episode_0045 | right_wrist_view | non-static | 4/20 | 2 | /home/ubuntu/data/curated_samples/success/brush_the_white_plastic_board/2026-06-04/umi/yuanhao/episode_0045 |
| brush_the_white_plastic_board/2026-06-04/umi/yuanhao/episode_0056 | left_wrist_view | non-static | 5/22 | 2 | /home/ubuntu/data/curated_samples/success/brush_the_white_plastic_board/2026-06-04/umi/yuanhao/episode_0056 |
| brush_the_white_plastic_board/2026-06-05/umi/penglizhuan/episode_0028 | left_wrist_view | non-static | 13/24 | 0 | /home/ubuntu/data/curated_samples/success/brush_the_white_plastic_board/2026-06-05/umi/penglizhuan/episode_0028 |
| brush_water_on_the_clay/2026-05-28/umi/leijunyang/episode_0077 | right_wrist_view | non-static | 11/15 | 3 | /home/ubuntu/data/curated_samples/success/brush_water_on_the_clay/2026-05-28/umi/leijunyang/episode_0077 |
| brush_water_on_the_clay/2026-05-29/umi/wangpinye/episode_0022 | left_wrist_view | non-static | 5/22 | 0 | /home/ubuntu/data/curated_samples/success/brush_water_on_the_clay/2026-05-29/umi/wangpinye/episode_0022 |
| brush_water_on_the_clay/2026-05-29/umi/wangpinye/episode_0023 | left_wrist_view | non-static | 9/21 | 1 | /home/ubuntu/data/curated_samples/success/brush_water_on_the_clay/2026-05-29/umi/wangpinye/episode_0023 |
| brush_water_on_the_clay/2026-05-31/umi/heqi/episode_0026 | right_wrist_view | non-static | 5/24 | 0 | /home/ubuntu/data/curated_samples/success/brush_water_on_the_clay/2026-05-31/umi/heqi/episode_0026 |
| brush_water_on_the_clay/2026-06-01/umi/chenjiankang/episode_0016 | left_wrist_view | non-static | 9/23 | 1 | /home/ubuntu/data/curated_samples/success/brush_water_on_the_clay/2026-06-01/umi/chenjiankang/episode_0016 |
| ... | ... | ... | ... | ... | 1375 more rows in view_static_summary.csv |

## Static Views

| sample | view | verdict | moved/valid | strong-local | path |
| --- | --- | --- | --- | --- | --- |
| add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-04/umi/yangwenzhe/episode_0018 | left_wrist_view | static | 1/21 | 2 | /home/ubuntu/data/curated_samples/success/add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-04/umi/yangwenzhe/episode_0018 |
| add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-10/umi/liuguanjun/episode_0002 | left_wrist_view | static | 0/23 | 0 | /home/ubuntu/data/curated_samples/success/add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-10/umi/liuguanjun/episode_0002 |
| add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-16/umi/wangpinye/episode_0037 | left_wrist_view | static | 2/23 | 1 | /home/ubuntu/data/curated_samples/success/add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack/2026-06-16/umi/wangpinye/episode_0037 |
| assemble_battery/2026-05-28/umi/nanyaqiang/episode_0123 | left_wrist_view | static | 0/24 | 0 | /home/ubuntu/data/curated_samples/success/assemble_battery/2026-05-28/umi/nanyaqiang/episode_0123 |
| assemble_battery/2026-05-28/umi/nanyaqiang/episode_0125 | left_wrist_view | static | 0/24 | 0 | /home/ubuntu/data/curated_samples/success/assemble_battery/2026-05-28/umi/nanyaqiang/episode_0125 |
| assemble_battery/2026-06-07/umi/shixuewen/episode_0005 | left_wrist_view | static | 2/20 | 2 | /home/ubuntu/data/curated_samples/success/assemble_battery/2026-06-07/umi/shixuewen/episode_0005 |
| assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0017 | left_wrist_view | static | 2/18 | 1 | /home/ubuntu/data/curated_samples/success/assemble_the_remote_controller_put_into_drawer/2026-06-03/umi/liangxin/episode_0017 |
| bag_the_fruit/2026-06-12/umi/chenjiankang/episode_0041 | left_wrist_view | static | 0/24 | 0 | /home/ubuntu/data/curated_samples/success/bag_the_fruit/2026-06-12/umi/chenjiankang/episode_0041 |
| bag_the_fruit/2026-06-15/umi/panlei/episode_0038 | left_wrist_view | static | 1/23 | 0 | /home/ubuntu/data/curated_samples/success/bag_the_fruit/2026-06-15/umi/panlei/episode_0038 |
| bind_and_secure_the_socks/2026-06-15/umi/lihaoran/episode_0050 | right_wrist_view | static | 1/21 | 0 | /home/ubuntu/data/curated_samples/success/bind_and_secure_the_socks/2026-06-15/umi/lihaoran/episode_0050 |
| bind_and_secure_the_socks/2026-06-15/umi/lihaoran/episode_0056 | left_wrist_view | static | 3/22 | 2 | /home/ubuntu/data/curated_samples/success/bind_and_secure_the_socks/2026-06-15/umi/lihaoran/episode_0056 |
| brush_the_white_plastic_board/2026-06-03/umi/lanpengfei/episode_0004 | left_wrist_view | static | 0/24 | 0 | /home/ubuntu/data/curated_samples/success/brush_the_white_plastic_board/2026-06-03/umi/lanpengfei/episode_0004 |
| brush_the_white_plastic_board/2026-06-03/umi/lanpengfei/episode_0005 | left_wrist_view | static | 1/22 | 1 | /home/ubuntu/data/curated_samples/success/brush_the_white_plastic_board/2026-06-03/umi/lanpengfei/episode_0005 |
| brush_the_white_plastic_board/2026-06-03/umi/yuhailong/episode_0004 | left_wrist_view | static | 2/23 | 0 | /home/ubuntu/data/curated_samples/success/brush_the_white_plastic_board/2026-06-03/umi/yuhailong/episode_0004 |
| brush_the_white_plastic_board/2026-06-03/umi/yuhailong/episode_0004 | right_wrist_view | static | 3/24 | 0 | /home/ubuntu/data/curated_samples/success/brush_the_white_plastic_board/2026-06-03/umi/yuhailong/episode_0004 |
| brush_the_white_plastic_board/2026-06-04/umi/duanzongyu/episode_0037 | left_wrist_view | static | 0/23 | 0 | /home/ubuntu/data/curated_samples/success/brush_the_white_plastic_board/2026-06-04/umi/duanzongyu/episode_0037 |
| brush_the_white_plastic_board/2026-06-04/umi/duanzongyu/episode_0037 | right_wrist_view | static | 1/22 | 1 | /home/ubuntu/data/curated_samples/success/brush_the_white_plastic_board/2026-06-04/umi/duanzongyu/episode_0037 |
| brush_the_white_plastic_board/2026-06-04/umi/yuanhao/episode_0045 | left_wrist_view | static | 0/24 | 0 | /home/ubuntu/data/curated_samples/success/brush_the_white_plastic_board/2026-06-04/umi/yuanhao/episode_0045 |
| brush_the_white_plastic_board/2026-06-05/umi/penglizhuan/episode_0028 | right_wrist_view | static | 2/22 | 1 | /home/ubuntu/data/curated_samples/success/brush_the_white_plastic_board/2026-06-05/umi/penglizhuan/episode_0028 |
| brush_water_on_the_clay/2026-05-28/umi/leijunyang/episode_0077 | left_wrist_view | static | 0/22 | 2 | /home/ubuntu/data/curated_samples/success/brush_water_on_the_clay/2026-05-28/umi/leijunyang/episode_0077 |
| brush_water_on_the_clay/2026-05-31/umi/heqi/episode_0026 | left_wrist_view | static | 1/24 | 0 | /home/ubuntu/data/curated_samples/success/brush_water_on_the_clay/2026-05-31/umi/heqi/episode_0026 |
| brush_water_on_the_clay/2026-06-17/umi/yuexichen/episode_0025 | left_wrist_view | static | 2/20 | 1 | /home/ubuntu/data/curated_samples/success/brush_water_on_the_clay/2026-06-17/umi/yuexichen/episode_0025 |
| classification_of_building_block_colors/2026-06-01/umi/xiangming/episode_0058 | left_wrist_view | static | 3/24 | 0 | /home/ubuntu/data/curated_samples/success/classification_of_building_block_colors/2026-06-01/umi/xiangming/episode_0058 |
| classification_of_building_block_colors/2026-06-01/umi/xiangming/episode_0059 | left_wrist_view | static | 1/23 | 1 | /home/ubuntu/data/curated_samples/success/classification_of_building_block_colors/2026-06-01/umi/xiangming/episode_0059 |
| classify_cables/2026-06-01/umi/zhangpengfei/episode_0055 | left_wrist_view | static | 3/22 | 1 | /home/ubuntu/data/curated_samples/success/classify_cables/2026-06-01/umi/zhangpengfei/episode_0055 |
| classify_cables/2026-06-17/umi/huangzhenyong/episode_0051 | left_wrist_view | static | 2/16 | 1 | /home/ubuntu/data/curated_samples/success/classify_cables/2026-06-17/umi/huangzhenyong/episode_0051 |
| classify_the_battery/2026-05-31/umi/huangfuxiang/episode_0062 | left_wrist_view | static | 1/24 | 0 | /home/ubuntu/data/curated_samples/success/classify_the_battery/2026-05-31/umi/huangfuxiang/episode_0062 |
| classify_the_battery/2026-06-17/umi/huangzhenyong/episode_0086 | left_wrist_view | static | 0/19 | 2 | /home/ubuntu/data/curated_samples/success/classify_the_battery/2026-06-17/umi/huangzhenyong/episode_0086 |
| classify_the_battery/2026-06-17/umi/huangzhenyong/episode_0105 | left_wrist_view | static | 0/20 | 2 | /home/ubuntu/data/curated_samples/success/classify_the_battery/2026-06-17/umi/huangzhenyong/episode_0105 |
| clean_the_chemistry_suit/2026-05-26/umi/lijiangping/episode_0038 | left_wrist_view | static | 0/19 | 2 | /home/ubuntu/data/curated_samples/success/clean_the_chemistry_suit/2026-05-26/umi/lijiangping/episode_0038 |
| clean_the_chemistry_suit/2026-05-26/umi/lijiangping/episode_0089 | left_wrist_view | static | 1/23 | 1 | /home/ubuntu/data/curated_samples/success/clean_the_chemistry_suit/2026-05-26/umi/lijiangping/episode_0089 |
| clean_the_chemistry_suit/2026-06-02/umi/wangliang/episode_0019 | left_wrist_view | static | 1/22 | 1 | /home/ubuntu/data/curated_samples/success/clean_the_chemistry_suit/2026-06-02/umi/wangliang/episode_0019 |
| clean_the_plate/2026-05-25/umi/wangshuai/episode_0037 | left_wrist_view | static | 3/23 | 0 | /home/ubuntu/data/curated_samples/success/clean_the_plate/2026-05-25/umi/wangshuai/episode_0037 |
| clean_the_porcelain_bowl_on_the_plate/2026-05-25/umi/nanyaqiang/episode_0019 | left_wrist_view | static | 2/23 | 0 | /home/ubuntu/data/curated_samples/success/clean_the_porcelain_bowl_on_the_plate/2026-05-25/umi/nanyaqiang/episode_0019 |
| clean_the_porcelain_bowl_on_the_plate/2026-05-25/umi/nanyaqiang/episode_0030 | left_wrist_view | static | 2/23 | 1 | /home/ubuntu/data/curated_samples/success/clean_the_porcelain_bowl_on_the_plate/2026-05-25/umi/nanyaqiang/episode_0030 |
| clean_the_porcelain_bowl_on_the_plate/2026-05-28/umi/wuhao/episode_0031 | left_wrist_view | static | 1/18 | 2 | /home/ubuntu/data/curated_samples/success/clean_the_porcelain_bowl_on_the_plate/2026-05-28/umi/wuhao/episode_0031 |
| clean_the_shredded_paper/2026-05-25/umi/wanganqin/episode_0053 | left_wrist_view | static | 2/23 | 1 | /home/ubuntu/data/curated_samples/success/clean_the_shredded_paper/2026-05-25/umi/wanganqin/episode_0053 |
| clean_the_shredded_paper/2026-05-25/umi/wanganqin/episode_0078 | left_wrist_view | static | 3/23 | 1 | /home/ubuntu/data/curated_samples/success/clean_the_shredded_paper/2026-05-25/umi/wanganqin/episode_0078 |
| clean_the_shredded_paper/2026-05-28/umi/lihaoran/episode_0030 | right_wrist_view | static | 3/22 | 1 | /home/ubuntu/data/curated_samples/success/clean_the_shredded_paper/2026-05-28/umi/lihaoran/episode_0030 |
| clean_the_shredded_paper/2026-06-03/umi/tianchangwu/episode_0054 | left_wrist_view | static | 2/23 | 0 | /home/ubuntu/data/curated_samples/success/clean_the_shredded_paper/2026-06-03/umi/tianchangwu/episode_0054 |
| clean_the_table/2026-05-30/umi/wangliang/episode_0054 | left_wrist_view | static | 3/24 | 0 | /home/ubuntu/data/curated_samples/success/clean_the_table/2026-05-30/umi/wangliang/episode_0054 |
| clean_the_table/2026-06-12/umi/wangbo/episode_0061 | left_wrist_view | static | 1/22 | 1 | /home/ubuntu/data/curated_samples/success/clean_the_table/2026-06-12/umi/wangbo/episode_0061 |
| clean_the_table/2026-06-15/umi/liangxin/episode_0058 | right_wrist_view | static | 2/17 | 0 | /home/ubuntu/data/curated_samples/success/clean_the_table/2026-06-15/umi/liangxin/episode_0058 |
| cleaning_and_putting_away_desk_trash/2026-06-08/umi/lipengfei/episode_0050 | left_wrist_view | static | 1/21 | 1 | /home/ubuntu/data/curated_samples/success/cleaning_and_putting_away_desk_trash/2026-06-08/umi/lipengfei/episode_0050 |
| clip_chips/2026-05-28/umi/yebin/episode_0079 | left_wrist_view | static | 1/24 | 0 | /home/ubuntu/data/curated_samples/success/clip_chips/2026-05-28/umi/yebin/episode_0079 |
| clip_chips/2026-06-07/umi/panlei/episode_0009 | left_wrist_view | static | 0/24 | 0 | /home/ubuntu/data/curated_samples/success/clip_chips/2026-06-07/umi/panlei/episode_0009 |
| cover_the_whiteboard_by_sketchpaper/2026-06-10/umi/likun/episode_0053 | left_wrist_view | static | 3/22 | 1 | /home/ubuntu/data/curated_samples/success/cover_the_whiteboard_by_sketchpaper/2026-06-10/umi/likun/episode_0053 |
| cover_the_whiteboard_by_sketchpaper/2026-06-16/umi/panlei/episode_0041 | left_wrist_view | static | 0/24 | 0 | /home/ubuntu/data/curated_samples/success/cover_the_whiteboard_by_sketchpaper/2026-06-16/umi/panlei/episode_0041 |
| draw_a_circle_on_the_whiteboard/2026-05-29/umi/wangliang/episode_0013 | left_wrist_view | static | 2/21 | 2 | /home/ubuntu/data/curated_samples/success/draw_a_circle_on_the_whiteboard/2026-05-29/umi/wangliang/episode_0013 |
| draw_a_circle_on_the_whiteboard/2026-05-29/umi/wangliang/episode_0013 | right_wrist_view | static | 0/19 | 2 | /home/ubuntu/data/curated_samples/success/draw_a_circle_on_the_whiteboard/2026-05-29/umi/wangliang/episode_0013 |
| draw_a_circle_on_the_whiteboard/2026-05-29/umi/wangliang/episode_0017 | left_wrist_view | static | 2/24 | 0 | /home/ubuntu/data/curated_samples/success/draw_a_circle_on_the_whiteboard/2026-05-29/umi/wangliang/episode_0017 |
| draw_a_circle_on_the_whiteboard/2026-06-04/umi/wuhao/episode_0033 | right_wrist_view | static | 0/23 | 0 | /home/ubuntu/data/curated_samples/success/draw_a_circle_on_the_whiteboard/2026-06-04/umi/wuhao/episode_0033 |
| draw_a_circle_on_the_whiteboard/2026-06-08/umi/huangfuxiang/episode_0052 | left_wrist_view | static | 2/22 | 1 | /home/ubuntu/data/curated_samples/success/draw_a_circle_on_the_whiteboard/2026-06-08/umi/huangfuxiang/episode_0052 |
| draw_salt_water_with_a_dropper/2026-06-04/umi/lukele/episode_0226 | left_wrist_view | static | 0/24 | 0 | /home/ubuntu/data/curated_samples/success/draw_salt_water_with_a_dropper/2026-06-04/umi/lukele/episode_0226 |
| draw_salt_water_with_a_dropper/2026-06-10/umi/lijiangping/episode_0040 | left_wrist_view | static | 0/24 | 0 | /home/ubuntu/data/curated_samples/success/draw_salt_water_with_a_dropper/2026-06-10/umi/lijiangping/episode_0040 |
| dropper_continuously_drips_liquid_10_times/2026-05-26/umi/huangmeiting/episode_0067 | left_wrist_view | static | 1/24 | 0 | /home/ubuntu/data/curated_samples/success/dropper_continuously_drips_liquid_10_times/2026-05-26/umi/huangmeiting/episode_0067 |
| dropper_continuously_drips_liquid_10_times/2026-06-04/umi/chenxu/episode_0019 | left_wrist_view | static | 3/24 | 0 | /home/ubuntu/data/curated_samples/success/dropper_continuously_drips_liquid_10_times/2026-06-04/umi/chenxu/episode_0019 |
| dropper_continuously_drips_liquid_10_times/2026-06-08/umi/liuguanjun/episode_0072 | left_wrist_view | static | 0/24 | 0 | /home/ubuntu/data/curated_samples/success/dropper_continuously_drips_liquid_10_times/2026-06-08/umi/liuguanjun/episode_0072 |
| dropper_continuously_drips_liquid_10_times/2026-06-08/umi/liuguanjun/episode_0084 | left_wrist_view | static | 0/24 | 0 | /home/ubuntu/data/curated_samples/success/dropper_continuously_drips_liquid_10_times/2026-06-08/umi/liuguanjun/episode_0084 |
| dropper_continuously_drips_liquid_10_times/2026-06-09/umi/liangxin/episode_0015 | left_wrist_view | static | 1/23 | 1 | /home/ubuntu/data/curated_samples/success/dropper_continuously_drips_liquid_10_times/2026-06-09/umi/liangxin/episode_0015 |
| dropper_continuously_drips_liquid_10_times/2026-06-09/umi/liangxin/episode_0065 | left_wrist_view | static | 2/23 | 0 | /home/ubuntu/data/curated_samples/success/dropper_continuously_drips_liquid_10_times/2026-06-09/umi/liangxin/episode_0065 |
| dropper_continuously_drips_liquid_10_times/2026-06-09/umi/liangxin/episode_0072 | left_wrist_view | static | 2/23 | 1 | /home/ubuntu/data/curated_samples/success/dropper_continuously_drips_liquid_10_times/2026-06-09/umi/liangxin/episode_0072 |
| flatten_the_clay/2026-06-03/umi/liangxin/episode_0013 | left_wrist_view | static | 1/22 | 2 | /home/ubuntu/data/curated_samples/success/flatten_the_clay/2026-06-03/umi/liangxin/episode_0013 |
| flatten_the_clay/2026-06-03/umi/liangxin/episode_0013 | right_wrist_view | static | 2/14 | 1 | /home/ubuntu/data/curated_samples/success/flatten_the_clay/2026-06-03/umi/liangxin/episode_0013 |
| flatten_the_clay/2026-06-10/umi/guonan/episode_0019 | left_wrist_view | static | 2/22 | 0 | /home/ubuntu/data/curated_samples/success/flatten_the_clay/2026-06-10/umi/guonan/episode_0019 |
| folding_windmill/2026-05-26/umi/oushiqiang/episode_0088 | left_wrist_view | static | 1/14 | 2 | /home/ubuntu/data/curated_samples/success/folding_windmill/2026-05-26/umi/oushiqiang/episode_0088 |
| inhale_the_liquid_into_the_test_tube/2026-06-03/umi/wuhao/episode_0018 | right_wrist_view | static | 2/23 | 1 | /home/ubuntu/data/curated_samples/success/inhale_the_liquid_into_the_test_tube/2026-06-03/umi/wuhao/episode_0018 |
| inhale_the_liquid_into_the_test_tube/2026-06-04/umi/liangxin/episode_0016 | left_wrist_view | static | 3/23 | 1 | /home/ubuntu/data/curated_samples/success/inhale_the_liquid_into_the_test_tube/2026-06-04/umi/liangxin/episode_0016 |
| inhale_the_liquid_into_the_test_tube/2026-06-04/umi/liangxin/episode_0021 | left_wrist_view | static | 0/24 | 0 | /home/ubuntu/data/curated_samples/success/inhale_the_liquid_into_the_test_tube/2026-06-04/umi/liangxin/episode_0021 |
| inhale_the_liquid_into_the_test_tube/2026-06-08/umi/guonan/episode_0112 | left_wrist_view | static | 2/23 | 1 | /home/ubuntu/data/curated_samples/success/inhale_the_liquid_into_the_test_tube/2026-06-08/umi/guonan/episode_0112 |
| inhale_the_liquid_into_the_test_tube/2026-06-17/umi/chenxu/episode_0100 | left_wrist_view | static | 0/24 | 0 | /home/ubuntu/data/curated_samples/success/inhale_the_liquid_into_the_test_tube/2026-06-17/umi/chenxu/episode_0100 |
| lint_roller_remove_dirt/2026-06-09/umi/wangpinye/episode_0044 | left_wrist_view | static | 1/21 | 0 | /home/ubuntu/data/curated_samples/success/lint_roller_remove_dirt/2026-06-09/umi/wangpinye/episode_0044 |
| lint_roller_remove_dirt/2026-06-09/umi/wangpinye/episode_0044 | right_wrist_view | static | 1/23 | 0 | /home/ubuntu/data/curated_samples/success/lint_roller_remove_dirt/2026-06-09/umi/wangpinye/episode_0044 |
| lint_roller_remove_dirt/2026-06-09/umi/yangwenzhe/episode_0020 | right_wrist_view | static | 1/22 | 1 | /home/ubuntu/data/curated_samples/success/lint_roller_remove_dirt/2026-06-09/umi/yangwenzhe/episode_0020 |
| lint_roller_remove_dirt/2026-06-12/umi/lukele/episode_0011 | right_wrist_view | static | 1/22 | 0 | /home/ubuntu/data/curated_samples/success/lint_roller_remove_dirt/2026-06-12/umi/lukele/episode_0011 |
| lint_roller_remove_dirt/2026-06-14/umi/leijunyang/episode_0006 | left_wrist_view | static | 3/22 | 2 | /home/ubuntu/data/curated_samples/success/lint_roller_remove_dirt/2026-06-14/umi/leijunyang/episode_0006 |
| mixing_colors_on_the_palette/2026-05-28/umi/huangfuxiang/episode_0031 | left_wrist_view | static | 1/23 | 0 | /home/ubuntu/data/curated_samples/success/mixing_colors_on_the_palette/2026-05-28/umi/huangfuxiang/episode_0031 |
| mixing_colors_on_the_palette/2026-06-09/umi/penglizhuan/episode_0026 | left_wrist_view | static | 2/24 | 0 | /home/ubuntu/data/curated_samples/success/mixing_colors_on_the_palette/2026-06-09/umi/penglizhuan/episode_0026 |
| mixing_colors_on_the_palette/2026-06-12/umi/wangliang/episode_0012 | left_wrist_view | static | 1/24 | 0 | /home/ubuntu/data/curated_samples/success/mixing_colors_on_the_palette/2026-06-12/umi/wangliang/episode_0012 |
| move_the_pot_to_the_corner/2026-06-12/umi/liuguanjun/episode_0058 | left_wrist_view | static | 3/24 | 0 | /home/ubuntu/data/curated_samples/success/move_the_pot_to_the_corner/2026-06-12/umi/liuguanjun/episode_0058 |
| open_the_lid_of_a_teacup/2026-06-03/umi/bianyuchen/episode_0018 | left_wrist_view | static | 2/22 | 1 | /home/ubuntu/data/curated_samples/success/open_the_lid_of_a_teacup/2026-06-03/umi/bianyuchen/episode_0018 |
| open_the_lid_of_a_teacup/2026-06-03/umi/bianyuchen/episode_0075 | left_wrist_view | static | 2/21 | 2 | /home/ubuntu/data/curated_samples/success/open_the_lid_of_a_teacup/2026-06-03/umi/bianyuchen/episode_0075 |
| open_the_lid_of_a_teacup/2026-06-05/umi/wuhuming/episode_0068 | left_wrist_view | static | 3/24 | 0 | /home/ubuntu/data/curated_samples/success/open_the_lid_of_a_teacup/2026-06-05/umi/wuhuming/episode_0068 |
| open_the_lid_of_a_teacup/2026-06-07/umi/zhangyuqi/episode_0016 | right_wrist_view | static | 2/23 | 1 | /home/ubuntu/data/curated_samples/success/open_the_lid_of_a_teacup/2026-06-07/umi/zhangyuqi/episode_0016 |
| open_the_lid_of_a_teacup/2026-06-15/umi/nanyaqiang/episode_0018 | right_wrist_view | static | 2/21 | 2 | /home/ubuntu/data/curated_samples/success/open_the_lid_of_a_teacup/2026-06-15/umi/nanyaqiang/episode_0018 |
| open_the_lid_of_a_teacup/2026-06-15/umi/nanyaqiang/episode_0080 | left_wrist_view | static | 3/21 | 1 | /home/ubuntu/data/curated_samples/success/open_the_lid_of_a_teacup/2026-06-15/umi/nanyaqiang/episode_0080 |
| open_the_lid_of_a_teacup/2026-06-15/umi/nanyaqiang/episode_0080 | right_wrist_view | static | 3/21 | 1 | /home/ubuntu/data/curated_samples/success/open_the_lid_of_a_teacup/2026-06-15/umi/nanyaqiang/episode_0080 |
| organize_the_cable/2026-06-09/umi/pengshasha/episode_0002 | left_wrist_view | static | 0/22 | 0 | /home/ubuntu/data/curated_samples/success/organize_the_cable/2026-06-09/umi/pengshasha/episode_0002 |
| organize_the_cable/2026-06-09/umi/pengshasha/episode_0017 | left_wrist_view | static | 3/23 | 0 | /home/ubuntu/data/curated_samples/success/organize_the_cable/2026-06-09/umi/pengshasha/episode_0017 |
| pack_eggs/2026-06-12/umi/lijiangping/episode_0006 | left_wrist_view | static | 0/24 | 0 | /home/ubuntu/data/curated_samples/success/pack_eggs/2026-06-12/umi/lijiangping/episode_0006 |
| paper_cup_packaged_imitation_rice/2026-05-28/umi/wangpinye/episode_0074 | left_wrist_view | static | 1/20 | 1 | /home/ubuntu/data/curated_samples/success/paper_cup_packaged_imitation_rice/2026-05-28/umi/wangpinye/episode_0074 |
| paper_cup_packaged_imitation_rice/2026-06-03/umi/yebin/episode_0034 | left_wrist_view | static | 1/21 | 1 | /home/ubuntu/data/curated_samples/success/paper_cup_packaged_imitation_rice/2026-06-03/umi/yebin/episode_0034 |
| paper_cup_packaged_imitation_rice/2026-06-05/umi/lijiangping/episode_0018 | right_wrist_view | static | 3/22 | 1 | /home/ubuntu/data/curated_samples/success/paper_cup_packaged_imitation_rice/2026-06-05/umi/lijiangping/episode_0018 |
| paper_cup_packaged_imitation_rice/2026-06-10/umi/bianyuchen/episode_0032 | left_wrist_view | static | 1/23 | 0 | /home/ubuntu/data/curated_samples/success/paper_cup_packaged_imitation_rice/2026-06-10/umi/bianyuchen/episode_0032 |
| paper_cup_packaged_imitation_rice/2026-06-11/umi/lukele/episode_0024__sess_993fb5b9 | left_wrist_view | static | 1/12 | 2 | /home/ubuntu/data/curated_samples/success/paper_cup_packaged_imitation_rice/2026-06-11/umi/lukele/episode_0024__sess_993fb5b9 |
| paper_cup_packaged_imitation_rice/2026-06-16/umi/duanzongyu/episode_0092 | right_wrist_view | static | 0/24 | 0 | /home/ubuntu/data/curated_samples/success/paper_cup_packaged_imitation_rice/2026-06-16/umi/duanzongyu/episode_0092 |
| paper_cup_packaged_imitation_rice/2026-06-16/umi/huangzhenyong/episode_0062 | left_wrist_view | static | 2/22 | 1 | /home/ubuntu/data/curated_samples/success/paper_cup_packaged_imitation_rice/2026-06-16/umi/huangzhenyong/episode_0062 |
| paper_cups_pyramid/2026-06-16/umi/panlei/episode_0056 | left_wrist_view | static | 0/24 | 0 | /home/ubuntu/data/curated_samples/success/paper_cups_pyramid/2026-06-16/umi/panlei/episode_0056 |
| place_bowls_and_spoons/2026-06-02/umi/huangmeiting/episode_0013 | right_wrist_view | static | 1/15 | 2 | /home/ubuntu/data/curated_samples/success/place_bowls_and_spoons/2026-06-02/umi/huangmeiting/episode_0013 |
| place_cups_on_the_plate/2026-06-05/umi/likun/episode_0008 | left_wrist_view | static | 0/24 | 0 | /home/ubuntu/data/curated_samples/success/place_cups_on_the_plate/2026-06-05/umi/likun/episode_0008 |
| place_cups_on_the_plate/2026-06-05/umi/likun/episode_0048 | left_wrist_view | static | 0/24 | 0 | /home/ubuntu/data/curated_samples/success/place_cups_on_the_plate/2026-06-05/umi/likun/episode_0048 |
| place_the_building_blocks_into_the_pot/2026-06-04/umi/guonan/episode_0008 | left_wrist_view | static | 2/21 | 1 | /home/ubuntu/data/curated_samples/success/place_the_building_blocks_into_the_pot/2026-06-04/umi/guonan/episode_0008 |
| play_toys/2026-05-30/umi/liangxin/episode_0049 | right_wrist_view | static | 1/15 | 2 | /home/ubuntu/data/curated_samples/success/play_toys/2026-05-30/umi/liangxin/episode_0049 |
| play_toys/2026-05-30/umi/liangxin/episode_0081 | right_wrist_view | static | 1/23 | 0 | /home/ubuntu/data/curated_samples/success/play_toys/2026-05-30/umi/liangxin/episode_0081 |
| play_toys/2026-05-30/umi/liangxin/episode_0093 | right_wrist_view | static | 3/22 | 0 | /home/ubuntu/data/curated_samples/success/play_toys/2026-05-30/umi/liangxin/episode_0093 |
| play_toys/2026-06-16/umi/wangliang/episode_0021 | left_wrist_view | static | 2/18 | 2 | /home/ubuntu/data/curated_samples/success/play_toys/2026-06-16/umi/wangliang/episode_0021 |
| pour_the_rice_into_pot/2026-06-07/umi/xutao/episode_0041 | left_wrist_view | static | 1/23 | 1 | /home/ubuntu/data/curated_samples/success/pour_the_rice_into_pot/2026-06-07/umi/xutao/episode_0041 |
| pour_the_rice_into_pot/2026-06-13/umi/pengshasha/episode_0023 | left_wrist_view | static | 1/24 | 0 | /home/ubuntu/data/curated_samples/success/pour_the_rice_into_pot/2026-06-13/umi/pengshasha/episode_0023 |
| pour_the_rice_into_pot/2026-06-17/umi/chenxu/episode_0007 | left_wrist_view | static | 0/22 | 1 | /home/ubuntu/data/curated_samples/success/pour_the_rice_into_pot/2026-06-17/umi/chenxu/episode_0007 |
| pour_the_rice_into_pot/2026-06-17/umi/chenxu/episode_0016 | left_wrist_view | static | 0/23 | 1 | /home/ubuntu/data/curated_samples/success/pour_the_rice_into_pot/2026-06-17/umi/chenxu/episode_0016 |
| pour_the_salt/2026-05-29/umi/oushiqiang/episode_0024 | right_wrist_view | static | 3/22 | 2 | /home/ubuntu/data/curated_samples/success/pour_the_salt/2026-05-29/umi/oushiqiang/episode_0024 |
| pour_the_salt/2026-06-11/umi/lukele/episode_0066 | right_wrist_view | static | 2/22 | 0 | /home/ubuntu/data/curated_samples/success/pour_the_salt/2026-06-11/umi/lukele/episode_0066 |
| press_clay_onto_the_white_plastic_board/2026-06-02/umi/luochao/episode_0022 | left_wrist_view | static | 1/22 | 1 | /home/ubuntu/data/curated_samples/success/press_clay_onto_the_white_plastic_board/2026-06-02/umi/luochao/episode_0022 |
| press_clay_onto_the_white_plastic_board/2026-06-03/umi/lanpengfei/episode_0059 | right_wrist_view | static | 1/9 | 2 | /home/ubuntu/data/curated_samples/success/press_clay_onto_the_white_plastic_board/2026-06-03/umi/lanpengfei/episode_0059 |
| press_clay_onto_the_white_plastic_board/2026-06-10/umi/zhangyuqi/episode_0033 | right_wrist_view | static | 3/23 | 0 | /home/ubuntu/data/curated_samples/success/press_clay_onto_the_white_plastic_board/2026-06-10/umi/zhangyuqi/episode_0033 |
| put_battery_into_box/2026-06-05/umi/wuhao/episode_0058 | left_wrist_view | static | 0/22 | 1 | /home/ubuntu/data/curated_samples/success/put_battery_into_box/2026-06-05/umi/wuhao/episode_0058 |
| put_battery_into_box_and_close/2026-05-28/umi/wanganqin/episode_0059 | left_wrist_view | static | 0/20 | 1 | /home/ubuntu/data/curated_samples/success/put_battery_into_box_and_close/2026-05-28/umi/wanganqin/episode_0059 |
| put_battery_on_plate/2026-06-08/umi/penglizhuan/episode_0057 | left_wrist_view | static | 1/24 | 0 | /home/ubuntu/data/curated_samples/success/put_battery_on_plate/2026-06-08/umi/penglizhuan/episode_0057 |
| put_battery_on_plate/2026-06-12/umi/wangshuai/episode_0027 | left_wrist_view | static | 1/24 | 0 | /home/ubuntu/data/curated_samples/success/put_battery_on_plate/2026-06-12/umi/wangshuai/episode_0027 |
| put_cups_in_line/2026-06-05/umi/huangmeiting/episode_0028 | right_wrist_view | static | 0/22 | 1 | /home/ubuntu/data/curated_samples/success/put_cups_in_line/2026-06-05/umi/huangmeiting/episode_0028 |
| ... | ... | ... | ... | ... | 113 more rows in view_static_summary.csv |

## Output Files

- `view_static_summary.csv`: main view-level binary classification
- `view_static_summary.json`: main view-level binary classification with rule metadata
- `sample_view_summary.csv`: compact left/right columns for reference
- `static_check_results.csv/json`: raw detector metrics normalized to static/non-static labels
